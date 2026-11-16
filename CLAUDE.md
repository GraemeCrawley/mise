# CLAUDE.md — Mise

Household meal-planning app. **Read `docs/SPEC.md` and `README.md` first** for the why; this file is the how.

## Stack

- Python 3.12, FastAPI, SQLModel, Alembic, SQLite (+ FTS5 planned), Pint, APScheduler
- `uv` for deps (`uv run pytest`, `uv run uvicorn …`)
- Tailwind via CDN at v2.2.19 — **no JIT, no v3 syntax** (no `max-h-[80vh]`, no `bg-black/40` slash-opacity, no arbitrary values). Use inline `style="…"` for arbitrary values.
- Strangetom (CRF ingredient parser) + Claude Haiku for LLM verify

## Layout

```
mise/
  main.py                  # FastAPI wiring; routers mounted here
  models.py                # SQLModel tables (single file)
  db.py                    # engine + session
  services/                # pure-Python domain; no HTTP, no LLM in some
    ingredient_parser.py   # strangetom + Pint + canonical_resolver
    llm_verify.py          # Haiku one-call verify, cached in IngredientAlias
    shopping_aggregator.py # pure aggregation; three-rule display via buy_units
    buy_units.py           # curated buy-unit table (bridges + rollup) for shopping
    planner.py             # CookingSession + SlotAssignment placement
    recipe_exporter.py     # JSON export to recipes-export/ (git-tracked)
    xlsx_importer.py       # legacy bootstrap path
  adapters/rest/           # thin FastAPI routers; one per resource
    recipes.py, preview.py, planner.py, shopping.py, admin.py
migrations/versions/       # Alembic — linear chain required
tests/                     # pytest, ephemeral in-memory SQLite per test
docs/                      # SPEC.md is authoritative; see also api-surface.md
scripts/                   # ad-hoc utilities (smoke_parser, backfills)
recipes-export/            # JSON exports, **git-tracked** as off-site backup
legacy/                    # original Sheets/Apps Script setup, frozen
```

## Common commands

```bash
uv run pytest -x -q --ignore=tests/test_parser_corpus.py   # fast suite, ~30s
uv run pytest tests/test_planner.py -x -q                   # single file
docker compose build mise && docker compose up -d mise      # rebuild + restart
docker logs mise --tail 30                                  # check startup / migration
uv run alembic heads                                        # MUST be one line
uv run alembic upgrade head
```

## Gotchas (the non-obvious things)

- **PARSER_VERSION** (top of `services/ingredient_parser.py`) — bumping invalidates `ParserCache`. Bump on any parser logic change so existing rows re-parse.
- **Alembic chain must stay linear.** Two migrations parented off the same revision = "Multiple head revisions" and the container restart-loops. Always point new migrations at `alembic heads`, not what looks like the latest in the file listing.
- **IngredientAlias has three sources**: `auto` (parser-generated), `llm` (verify call), `manual` (operator override). Manual is sacred — never overwrite.
- **LLM verify has a grounding check.** At least one substantive word from `canonical_name` must appear in `source_line` (accent-stripped both sides). Hallucinated canonicals (`vanilla bean` → `canned_beans`) get rejected and fall back to deterministic. See `_llm_output_grounded_in_source`.
- **Recipe exports under `recipes-export/` are git-tracked.** Tests that hit `export_recipe` need `monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))` or they'll pollute the repo.
- **Container code is baked into the image.** Edits to `mise/` require `docker compose build mise` to take effect — `scripts/` and `docs/` are bind-mounted but the app code isn't.
- **Tests use ephemeral SQLite via `conftest.py`.** No shared fixtures between tests; each test creates its own household if needed.

## Adding a feature — the pattern

1. **Service** in `mise/services/` — pure functions, takes `db: Session`, raises `ValueError` for caller errors. Tested in isolation.
2. **REST adapter** in `mise/adapters/rest/` — thin Pydantic-typed router that calls the service and translates exceptions to HTTPException.
3. **Wire** in `mise/main.py` (`app.include_router(...)`).
4. **HTML UI** in `mise/adapters/rest/preview.py` if there's an operator-facing surface. The preview is intentionally one big file; resist splitting until it actually hurts.
5. **Tests** in `tests/test_<thing>.py`. We're at ~860 passing; keep that going.

## Phase status (as of late May 2026)

- ✅ Phase 0 — import, preview, inline edit
- ✅ Phase 0.5 — shopping aggregator with solid density, multi-recipe shopping list
- ✅ Phase 1 — structured add (new recipe form, add-ingredient, add-step endpoints — bypass the parser)
- ✅ Planner v0 — placement with leftover auto-fill, day-grid UI, "build shopping list" link
- 🚧 Lauren's evaluation pass — surfacing data issues; parser-fix tickets land as commits
- ⏳ Today-view default landing, portion overrides, picker filters, templates (per spec, deferred)

## Conventions

- One-line comments on non-obvious WHY only. No docstring novellas.
- Commit messages: `feat:` / `fix:` / `chore:` prefix, then a clear short title. Body explains the *why*, not the diff.
- New ingredient-row writes go through the **slug** path (`_slug_canonical` in `recipes.py`) for structured input, OR the **parse → verify → cache** pipeline for blob input. Don't invent a third path.
- Aggregation key is `_normalize_canonical(canonical_name)` — applies alias map then singular-strip. Add aliases there when LLM verify produces drift.
- **Units are stored NATURAL** (as the recipe wrote them: cup stays cup, g stays g) since PARSER_VERSION v27. `to_canonical` only normalizes spelling — it no longer converts volume→ml / mass→g. Cross-unit shopping math lives in `services/buy_units.py` (a curated buy-unit table) and the aggregator's three rules: buy-unit table → within-dimension sum → show parts (`"2 cup + 250 g"`). `apply_category_preference`/`PREFERRED_UNITS_BY_CATEGORY` are RETIRED from the write path (kept only for parse-trace/tests/legacy) — don't reintroduce. The aggregator groups by canonical ONLY (not canonical+dimension) so a buy-unit ingredient combines across dimensions.
- **Showing a quantity? Use `services/measure.Measure`.** `Measure.of(qty, unit).html(cls)` renders a span that `static/measure.js` (loaded on every page) makes tappable for a unit dropdown and that the serving stepper rescales via `data-scale` + `Measure.render(el)`. `.text()` for plain strings. Don't hand-format qty+unit or add another conversion table — `measure.UNITS` is the one table (sent to the browser as JSON).
