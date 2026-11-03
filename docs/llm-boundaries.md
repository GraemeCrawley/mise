# LLM boundaries

## The principle

**LLMs touch the system at edges only. Outputs become data; data is forever; the LLM exits.**

The domain layer never calls an LLM. The grocery aggregator, planner, pantry deductor, and every REST endpoint are pure deterministic Python. This protects:

- **Cost** — LLM calls are one-time per input (parser cache), or per-MCP-call (small and bounded)
- **Latency** — hot paths are sub-100ms SQL queries
- **Reliability** — same data → same output, every time, forever
- **Auditability** — "why is X on the list" is answerable from rows, not from model temperature

## The five call sites

| Site | When | Cached? |
|---|---|---|
| `services.ingredient_parser.parse_line()` | Sheet sync writes new/changed recipe row; manual recipe form save | Yes, persistent (`ParserCache` keyed by raw text hash + `parser_version`) |
| `services.canonical_resolver.resolve()` | New ingredient name encountered | Yes, persistent (`IngredientAlias` table) |
| `services.recipe_importer.from_url()` | (Future) URL import action | Per-action |
| `services.intent_parser.parse_when()` / `parse_constraints()` | MCP tool boundary only | Per-call, small |
| `domain.suggest.fill_week()` and related | MCP/UI "suggest" buttons | Per-call; output is a *proposal*, never a write |

These are the only places that import an LLM client. Enforced via lint rule:

```toml
# pyproject.toml
[tool.ruff.lint.flake8-tidy-imports.banned-api]
"anthropic" = { msg = "LLM clients are only allowed in services/" }
"openai"    = { msg = "LLM clients are only allowed in services/" }
```

## Caching strategy

### Parser cache

```python
class ParserCache(SQLModel, table=True):
    raw_text_hash: str = Field(primary_key=True)
    raw_text: str
    result_json: str
    parser_version: str       # bump to invalidate
    created_at: datetime
```

**Behavior:**
- Hash = `sha256(normalize(raw_text))` where normalize strips trailing whitespace, collapses runs of spaces
- Hit → return parsed result, no LLM call
- Miss → LLM call → validate against Pydantic schema → write cache row → return
- `parser_version` bump = effective invalidation (writes go to new rows; old rows ignored until manual cleanup)

**Cost shape:** ~400 ingredient lines for the initial library import, perhaps $0.50 in tokens with Claude Haiku. Steady-state usage is near-zero — new recipes add ~10 cache misses each.

### Canonical name cache

```python
class IngredientAlias(SQLModel, table=True):
    display_name: str = Field(primary_key=True)
    canonical_name: str = Field(index=True)
    confidence: float
    source: str               # "manual"|"llm"|"seed"
```

**Behavior:**
- Lookup `display_name.lower()` in alias table
- Fall back to fuzzy match (≥0.95 similarity to existing canonical)
- LLM resolves only on first encounter with an unseen name
- Result is *cached forever*; manual overrides flip `source` to "manual"

### Seeds

Ship a `seeds/canonical_aliases.json` with ~200 obvious entries (pluralization, common synonyms). Loaded into the alias table on first boot. Cuts most of the initial LLM resolution work.

## Proposals, not writes

`suggest_week()` and similar agent-facing tools return a **proposal object** that the caller must explicitly apply:

```python
proposal = Suggester(db).fill_week(constraints)
proposal_id = store_proposal(proposal)
return {"proposal_id": ..., "summary": proposal.summary()}

# Later:
@server.tool()
async def apply_week_proposal(proposal_id: str) -> str: ...
```

Reason: an LLM should never silently mutate persistent state. Human confirms; then mutation happens via a deterministic domain call.

## Anti-patterns to avoid

- **Tools that call back into the LLM mid-execution.** A `plan_meal` tool that internally asks the LLM "what should I do next?" is wrong. Composition stays in the agent.
- **Caching LLM output in memory only.** Container restarts and bills resume.
- **Using LLMs in `domain/`.** If you find yourself wanting to, lift the LLM step into `services/` and return a deterministic input.
- **Letting LLMs choose recipe IDs / database keys directly.** They hallucinate. Always fuzzy-match against a known set.

## What to do when an LLM call fails

| Site | Failure mode | Behavior |
|---|---|---|
| Parser (sync) | Mark ingredient row as unparsed (`raw_text` populated, `qty/unit` null); flag recipe `needs_review` in SyncRun |
| Parser (form) | Show inline error in live-parse preview; block save until resolved |
| Canonical resolver | Use raw display_name as canonical (lowercase, underscored); flag for review |
| Intent parser (MCP) | Return helpful error to agent: "Couldn't parse 'when'. Try 'Tuesday dinner' or 'tomorrow lunch'." |
| Suggester | Return empty proposal with explanation |

Never silent failure. Never partial writes.
