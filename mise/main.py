"""FastAPI app. Adapters mounted as routers; domain stays import-free
of HTTP. MCP mount comes in Phase 3.

This file stays small on purpose — it's a wiring file, not a logic
file. Anything tempted to grow here belongs in adapters/ or services/.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from prometheus_client import make_asgi_app

from mise import __version__
from mise.adapters.rest import admin, app as consumer_app, planner, preview, recipes, shopping

log = logging.getLogger("mise")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Container startup / shutdown hooks. Alembic upgrade runs
    OUTSIDE the lifespan (entrypoint script in the Dockerfile / compose
    command) so a startup failure surfaces as a deploy failure, not a
    mid-flight crash."""
    log.info("mise %s starting", __version__)

    # Pre-warm strangetom — the CRF model takes ~5s to load on first
    # call. Doing it here moves that cost to container start instead
    # of the first /preview/parse-trace click (the user-facing slow path).
    try:
        from mise.services.ingredient_parser import _default_strangetom
        _default_strangetom("1 cup water")
        log.info("strangetom warm")
    except Exception as e:
        log.warning("strangetom pre-warm failed: %s", e)

    # Hourly SQLite snapshots. Disabled in tests + when DATABASE_URL
    # points at an in-memory DB (we'd snapshot nothing).
    scheduler = None
    import os as _os
    if "MISE_DISABLE_SCHEDULER" not in _os.environ and ":memory:" not in (
        _os.environ.get("DATABASE_URL", "")
    ):
        try:
            from mise.services.snapshot import start_scheduler

            scheduler = start_scheduler()
        except Exception as e:
            log.warning("snapshot scheduler failed to start: %s", e)

    yield

    if scheduler is not None:
        scheduler.shutdown(wait=False)
    log.info("mise shutdown")


app = FastAPI(
    title="Mise",
    version=__version__,
    description="Household meal-planning app. Tailnet-only / internal during Phase 0.",
    lifespan=lifespan,
    # Behind Traefik strip-prefix at /mise in prod; empty for dev. Tells
    # FastAPI to prepend the prefix when generating redirects + the
    # Swagger UI's openapi_url, so /docs and trailing-slash redirects
    # round-trip through the proxy correctly.
    root_path=os.environ.get("MISE_URL_PREFIX", ""),
)

# Prometheus /metrics (the existing infra Prometheus scrapes via the
# `prometheus.scrape=true` Docker label).
app.mount("/metrics", make_asgi_app())

# V2 frontend mockups — static HTML, served over Tailnet so the device
# (phone) can browse the design directions live. Lives under `docs/`
# because it's design artefacts, not app code.
#
# Using a regular GET handler rather than app.mount(StaticFiles(...))
# because Starlette's Mount path-matching prepends `root_path` (= "/mise"
# in prod), so the mount only fires at /mise/mockups before strip. After
# Traefik's strip-prefix middleware removes /mise/, the inbound path is
# /mockups/... and the mount no longer matches. APIRoute path matching
# isn't affected by root_path, so a handler works in both setups.
from fastapi.responses import FileResponse
from pathlib import Path as _Path

_MOCKUPS_DIR = _Path(__file__).resolve().parent.parent / "docs" / "mockups" / "v2"


_STATIC_DIR = _Path(__file__).resolve().parent / "static"


@app.get("/static/{filename}", include_in_schema=False)
def _static_file(filename: str) -> FileResponse:
    """Precompiled assets — Tailwind CSS bundle, etc. Treated as
    versioned: edits to mise/ require a rebuild anyway, so cache
    aggressively (Cloudflare default is fine)."""
    target = (_STATIC_DIR / filename).resolve()
    if not str(target).startswith(str(_STATIC_DIR)) or not target.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(target, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/sw.js", include_in_schema=False)
def _service_worker() -> FileResponse:
    """Service worker — served at the root of the app prefix so its
    default scope covers all of /mise/. No-cache because we want
    updates to the SW to ship on next visit. Service-Worker-Allowed
    header is set just in case the registration ever moves."""
    return FileResponse(
        _STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


@app.get("/mockups", include_in_schema=False)
def _mockups_index_redirect() -> RedirectResponse:
    # Force trailing slash so relative links (`editorial.html`) resolve
    # to /mockups/editorial.html rather than /editorial.html. FastAPI's
    # default redirect_slashes was bypassed by the explicit route.
    # PREFIX matches the mise URL prefix in prod (Traefik adds /mise back
    # on redirects that originate with root_path set).
    return RedirectResponse(url="mockups/", status_code=307)


@app.get("/mockups/", include_in_schema=False)
def _mockups_index() -> FileResponse:
    return FileResponse(_MOCKUPS_DIR / "index.html")


# Sidecar comment store for the V2 UX review doc. Testers leave notes
# in textareas that autosave here. Bind-mounted, so writes persist on host.
import json as _json
import re as _re
from fastapi import Request as _Request

_UX_COMMENTS_FILE = _MOCKUPS_DIR / "ux-review-comments.json"
_UX_KEY_RE = _re.compile(r"^[a-z0-9][a-z0-9\-]{0,80}$")


def _load_ux_comments() -> dict[str, str]:
    if not _UX_COMMENTS_FILE.is_file():
        return {}
    try:
        return _json.loads(_UX_COMMENTS_FILE.read_text())
    except _json.JSONDecodeError:
        return {}


@app.get("/mockups/ux-review/comments", include_in_schema=False)
def _ux_comments_get() -> dict[str, str]:
    return _load_ux_comments()


@app.put("/mockups/ux-review/comments/{key}", include_in_schema=False)
async def _ux_comments_put(key: str, req: _Request) -> dict[str, str]:
    if not _UX_KEY_RE.fullmatch(key):
        raise HTTPException(status_code=400, detail="invalid key")
    body = await req.json()
    value = body.get("value", "")
    if not isinstance(value, str) or len(value) > 8000:
        raise HTTPException(status_code=400, detail="invalid value")
    data = _load_ux_comments()
    if value.strip() == "":
        data.pop(key, None)
    else:
        data[key] = value
    _UX_COMMENTS_FILE.write_text(_json.dumps(data, indent=2, sort_keys=True))
    return {"status": "ok"}


@app.get("/mockups/{filename}", include_in_schema=False)
def _mockups_file(filename: str) -> FileResponse:
    # Defensive: only serve files that exist inside the mockups dir.
    # The path is bound by FastAPI to a single segment, so it can't
    # contain slashes; resolve() guards against any ".." in the name.
    target = (_MOCKUPS_DIR / filename).resolve()
    if not str(target).startswith(str(_MOCKUPS_DIR)) or not target.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(target)

# Adapters
app.include_router(admin.router)
app.include_router(recipes.router)
app.include_router(preview.router)
app.include_router(shopping.router)
app.include_router(planner.router)
app.include_router(consumer_app.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: process is up. Readiness can be a separate /readyz
    later when there's setup work to gate on."""
    return {"status": "ok", "version": __version__}
