"""Add-a-recipe drafter — text blob or URL in, draft Recipe out.

Two structuring paths, one persistence path:
  1. URL → fetch → schema.org/Recipe JSON-LD (deterministic, $0)
  2. anything else (or JSON-LD missing) → one Claude call
Both produce a DraftRecipe (header fields + ingredients/instructions
blobs in the section-header dialect that parse_blob/parse_instructions
already speak), which feeds upsert_recipe_from_blobs() — the same
parse → verify → persist pipeline the xlsx importer uses. No third path.
"""

from __future__ import annotations

import html as html_lib
import ipaddress
import json
import logging
import os
import re
import socket
from urllib.parse import urlparse

import anthropic
import httpx
from pydantic import BaseModel
from sqlmodel import Session, select

from mise.models import Recipe
from mise.services.llm_verify import _strip_accents
from mise.services.xlsx_importer import upsert_recipe_from_blobs

log = logging.getLogger("mise.recipe_drafter")


class DraftRecipe(BaseModel):
    title: str
    servings: int | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    total_minutes: int | None = None
    oven_temp_f: int | None = None  # only the LLM path fills this; JSON-LD has no temp field
    ingredients_blob: str
    instructions_blob: str
    source_url: str | None = None


# ---- JSON-LD path -----------------------------------------------------------

_LDJSON_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.S | re.I,
)
_ISO_DUR_RE = re.compile(r"^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:[\d.]+S)?)?$", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s: object) -> str:
    """HTML-entity unescape + tag strip + whitespace collapse."""
    if not isinstance(s, str):
        return ""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html_lib.unescape(s))).strip()


def _iso_minutes(s: object) -> int | None:
    if not isinstance(s, str):
        return None
    m = _ISO_DUR_RE.match(s.strip())
    if not m or not any(m.groups()):
        return None
    weeks, days, hours, mins = (int(g) if g else 0 for g in m.groups())
    total = weeks * 10080 + days * 1440 + hours * 60 + mins
    return total or None


def _yield_to_servings(y: object) -> int | None:
    if isinstance(y, bool):
        return None
    if isinstance(y, (int, float)):
        return int(y) or None
    if isinstance(y, list):
        for item in y:
            got = _yield_to_servings(item)
            if got:
                return got
        return None
    if isinstance(y, str):
        m = re.search(r"\d+", y)
        return int(m.group()) if m else None
    return None


def _find_recipe_node(data: object) -> dict | None:
    nodes = data if isinstance(data, list) else [data]
    for node in nodes:
        if not isinstance(node, dict):
            continue
        graph = node.get("@graph")
        if isinstance(graph, list):
            found = _find_recipe_node(graph)
            if found:
                return found
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and x.lower() == "recipe" for x in types):
            return node
    return None


def _instructions_blob(instr: object) -> str:
    """HowToStep / HowToSection / plain strings → blob. Section names
    become 'Name:' header lines so parse_instructions re-sections them."""
    lines: list[str] = []

    def walk(item: object) -> None:
        if isinstance(item, str):
            lines.append(_clean(item))
        elif isinstance(item, list):
            for sub in item:
                walk(sub)
        elif isinstance(item, dict):
            t = item.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(isinstance(x, str) and x.lower() == "howtosection" for x in types):
                name = _clean(item.get("name"))
                if name:
                    lines.append(f"{name.rstrip(':')}:")
                walk(item.get("itemListElement") or [])
            else:
                text = _clean(item.get("text") or item.get("name"))
                if text:
                    lines.append(text)

    walk(instr)
    return "\n".join(line for line in lines if line)


def extract_jsonld_recipe(html_doc: str, url: str) -> DraftRecipe | None:
    """Scan ld+json blocks for a schema.org Recipe. None when the page
    has no usable recipe (caller falls back to the LLM path)."""
    for m in _LDJSON_RE.finditer(html_doc):
        try:
            data = json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        node = _find_recipe_node(data)
        if node is None:
            continue
        title = _clean(node.get("name"))
        raw_ingredients = node.get("recipeIngredient") or node.get("ingredients") or []
        if isinstance(raw_ingredients, str):
            raw_ingredients = [raw_ingredients]
        ingredients = [_clean(i) for i in raw_ingredients if _clean(i)]
        if not title or not ingredients:
            continue
        return DraftRecipe(
            title=title,
            servings=_yield_to_servings(node.get("recipeYield")),
            prep_minutes=_iso_minutes(node.get("prepTime")),
            cook_minutes=_iso_minutes(node.get("cookTime")),
            total_minutes=_iso_minutes(node.get("totalTime")),
            ingredients_blob="\n".join(ingredients),
            instructions_blob=_instructions_blob(node.get("recipeInstructions")),
            source_url=url,
        )
    return None


# ---- LLM structuring path ---------------------------------------------------

_DRAFTER_SYSTEM = """You convert messy recipe text into a structured draft. \
The text may be dictated speech (filler words, run-ons, spoken corrections), \
a pasted recipe, or text scraped from a web page.

HARD RULES — violating any of these is a failure:
1. NEVER invent ingredients, steps, quantities, times, or temperatures that \
are not in the text. A field you cannot find is null.
2. Preserve the author's wording for ingredients and steps. Only fix \
dictation artifacts: drop filler words (um, uh, okay so), apply spoken \
corrections ("half a cup... actually make that a full cup" → "1 cup"), and \
convert spoken numbers to digits ("two tablespoons" → "2 tbsp" is WRONG — \
write "2 tablespoons"; never change the unit word itself).
3. Preserve section groupings. In both blobs a section header is a line \
ending in ":" with at most 5 words (e.g. "For the sauce:"). Reuse the \
author's own section names. No headers if the text has no sections.
4. ingredients_blob: exactly one ingredient per line.
5. instructions_blob: one step per line, in order, numbering stripped.
6. If the text contains no recognisable recipe, return an empty \
ingredients_blob.

oven_temp_f is Fahrenheit; convert from Celsius if the text uses °C \
(this conversion is allowed and required)."""


def structure_text(text: str, *, source_url: str | None = None) -> DraftRecipe | None:
    """One Claude call: messy text → DraftRecipe. None when the key is
    missing, the API fails, the output doesn't validate, or no recipe
    was found (empty ingredients)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("recipe_drafter: ANTHROPIC_API_KEY not set")
        return None
    client = anthropic.Anthropic(api_key=api_key)
    model = os.environ.get("MISE_DRAFTER_MODEL", "claude-haiku-4-5-20251001")

    tool = {
        "name": "draft_recipe",
        "description": "The structured recipe draft extracted from the text.",
        "input_schema": DraftRecipe.model_json_schema(),
    }
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            system=_DRAFTER_SYSTEM,
            tools=[tool],
            tool_choice={"type": "tool", "name": "draft_recipe"},
            messages=[{"role": "user", "content": text[:30000]}],
        )
    except Exception as e:
        log.warning("recipe_drafter API call failed: %s", e)
        return None

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "draft_recipe":
            try:
                draft = DraftRecipe.model_validate(block.input)
            except Exception as e:
                log.warning("recipe_drafter schema validation failed: %s", e)
                return None
            if not draft.ingredients_blob.strip() or not draft.title.strip():
                return None
            if not _title_grounded_in_text(draft.title, text):
                log.warning("recipe_drafter: title %r not grounded in input, rejecting", draft.title)
                return None
            if source_url and not draft.source_url:
                draft.source_url = source_url
            return draft
    return None


def _title_grounded_in_text(title: str, text: str) -> bool:
    # Same anti-hallucination pattern as llm_verify's grounding check:
    # a prompt-injected or invented title shares no substantive word
    # with the input. One-word overlap is deliberately lenient — the
    # LLM legitimately tidies titles ("mums lemon pasta" → "Mum's
    # Lemon Pasta").
    src = _strip_accents(text.lower())
    words = [_strip_accents(w) for w in re.findall(r"[a-zà-ÿ]+", title.lower()) if len(w) > 2]
    if not words:
        return True
    return any(w in src for w in words)

# ---- fetch + orchestration --------------------------------------------------

_URL_RE = re.compile(r"^https?://\S+$", re.I)

# Some recipe sites 403 generic clients; a desktop-browser UA is enough
# for the mainstream blogs. Paywalled pages usually still embed JSON-LD.
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en",
}

_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|table|section|article)[^>]*>", re.I
)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)


def _assert_public_host(url: str) -> None:
    # SSRF guard: the container shares a Docker network with other
    # services, so a crafted URL (or redirect) must not reach
    # loopback/private/link-local addresses.
    host = urlparse(url).hostname
    if not host:
        raise ValueError("That doesn't look like a web link.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError("Couldn't reach that page. Check the link and your connection.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("That link points somewhere this server can't fetch.")


def fetch_page(url: str) -> str:
    """GET the page. Raises ValueError with a user-facing message on
    any network/HTTP failure — the adapter maps that to a 400.
    Redirects are followed manually so every hop gets the SSRF check."""
    if not _URL_RE.match(url):
        raise ValueError("That doesn't look like a web link.")
    try:
        for _ in range(5):
            _assert_public_host(url)
            resp = httpx.get(url, headers=_FETCH_HEADERS, follow_redirects=False, timeout=20.0)
            if resp.is_redirect:
                url = str(httpx.URL(url).join(resp.headers.get("location", "")))
                if not _URL_RE.match(url):
                    raise ValueError("That doesn't look like a web link.")
                continue
            resp.raise_for_status()
            return resp.text
        raise ValueError("Too many redirects — couldn't read that page.")
    except httpx.HTTPStatusError as e:
        raise ValueError(f"The site answered {e.response.status_code} — couldn't read that page.") from e
    except httpx.HTTPError as e:
        raise ValueError("Couldn't reach that page. Check the link and your connection.") from e


def _html_to_text(html_doc: str) -> str:
    """Readable-ish text for the LLM fallback: scripts/styles gone,
    block tags → newlines, entities unescaped, capped for token cost."""
    s = _SCRIPT_STYLE_RE.sub(" ", html_doc)
    s = _BLOCK_TAG_RE.sub("\n", s)
    s = _TAG_RE.sub(" ", s)
    s = html_lib.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()[:30000]


def _unique_title(db: Session, title: str) -> str:
    """upsert_recipe_from_blobs matches on name_english and would
    UPDATE an existing recipe. Drafts must always create — suffix the
    title until it's free. Check-then-insert race is accepted: single
    uvicorn worker, two household users."""
    base, candidate, n = title.strip(), title.strip(), 2
    while db.exec(select(Recipe).where(Recipe.name_english == candidate)).first():
        candidate = f"{base} ({n})"
        n += 1
    return candidate


def create_draft(
    db: Session,
    *,
    text: str | None = None,
    url: str | None = None,
    llm_verify_mode: str = "first-seen",
) -> Recipe:
    """Text blob or URL → persisted draft Recipe (reviewed_at NULL).
    Raises ValueError with a user-facing message on every failure mode.
    Caller commits."""
    if text and not url and _URL_RE.match(text.strip()):
        url, text = text.strip(), None

    draft: DraftRecipe | None = None
    if url:
        html_doc = fetch_page(url.strip())
        draft = extract_jsonld_recipe(html_doc, url.strip())
        if draft is None:
            draft = structure_text(_html_to_text(html_doc), source_url=url.strip())
        if draft is None:
            raise ValueError("Couldn't find a recipe on that page.")
    elif text and text.strip():
        draft = structure_text(text)
        if draft is None:
            raise ValueError("Couldn't make a recipe out of that — try adding ingredients and steps.")
    else:
        raise ValueError("Paste a recipe or a link first.")

    return upsert_recipe_from_blobs(
        db,
        name_english=_unique_title(db, draft.title),
        name_original=None,
        language=None,
        prep_minutes=draft.prep_minutes,
        cook_minutes=draft.cook_minutes,
        total_minutes=draft.total_minutes,
        oven_temp_f=draft.oven_temp_f,
        servings=draft.servings or 4,
        ingredients_blob=draft.ingredients_blob,
        instructions_blob=draft.instructions_blob,
        source_url=draft.source_url,
        llm_verify_mode=llm_verify_mode,  # type: ignore[arg-type]
        keep_failed_lines=True,
    )
