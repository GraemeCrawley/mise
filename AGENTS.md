# AGENTS.md: working on Mise

Mise is a self-hosted household meal planner: recipe library, week plan, aisle-sorted shopping list and cook mode. README.md is the user-facing version; this is the short version for agents.

## Run it for a user

```bash
cp .env.example .env          # set TZ; ANTHROPIC_API_KEY is optional
docker compose up -d --build
curl -fsS http://localhost:${MISE_PORT:-8000}/healthz
python3 samples/seed.py http://localhost:${MISE_PORT:-8000}   # only if they want sample data
```

Ask the human only for things you can't decide: their timezone, whether they want an Anthropic API key (and to paste it into `.env` themselves), and whether other devices need access.

## Develop

```bash
uv sync
uv run pytest -q              # each test gets its own in-memory SQLite
uv run alembic heads          # must print exactly one line
```

- **Layout:** `mise/services/` is plain Python (no HTTP). `mise/adapters/rest/` holds thin FastAPI routers plus the server-rendered HTML pages (`app.py` is the household app, `preview.py` the older operator pages). Models are all in `mise/models.py`.
- **Pages are HTML inside Python f-strings.** In inline `<script>` blocks, double the braces, write `\\n` for a newline in a JS string, and avoid `\'`. `tests/test_page_scripts.py` syntax-checks every page's scripts with `node --check`, so run it after touching page JS.
- **Tailwind is v2.2 from a CDN** (no JIT). Use inline `style="…"` for anything Tailwind v2 doesn't have.
- **Bump `CACHE_NAME` in `mise/static/sw.js`** whenever page HTML/JS changes, or installed phones keep the old version.
- **Migrations:** Alembic, one linear chain. Point a new migration at the output of `alembic heads`.
- **Parser changes:** bump `PARSER_VERSION` in `mise/services/ingredient_parser.py` so cached parses refresh.
- **Units are stored as written** (cup stays cup). Shopping-list conversions live in `mise/services/buy_units.py`.
- Tests that save recipes must set `RECIPES_EXPORT_PATH` to a temp dir (`monkeypatch.setenv`).

## Rules

- **Mise has no authentication.** Don't bind it to a public interface or set up port forwarding. For remote access, recommend a VPN (e.g. Tailscale) or an authenticating proxy.
- **Never commit `.env`, `*.db` files or a real `recipes-export/`.** `.gitignore` covers them, so don't force-add.
- **Never run `docker compose down -v`** on a real install without explicit confirmation: it deletes all data.
- Sample data must stay made-up. Don't add recipes copied from websites to the repo.
- `ANTHROPIC_API_KEY` must stay optional: blank means the Claude features are off, with no errors.
