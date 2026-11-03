# Ingredient parsing

## The parser

Takes a freeform ingredient line like `"2 (15oz) cans crushed tomatoes, drained"` and returns structured fields.

```python
class ParsedIngredient(BaseModel):
    qty: float
    unit: str                    # canonical
    name: str                    # "crushed tomatoes"
    canonical_name: str          # "crushed_tomatoes"
    category: str                # "Pantry"
    prep_note: str | None        # "drained"
    size_descriptor: str | None  # "large"
    package_qty: float | None    # 15
    package_unit: str | None     # "oz"
    optional: bool               # "to taste", "optional"
    original_qty: float | None   # 2 (pre-unification)
    original_unit: str | None    # "can" (pre-unification)
```

**Approach: LLM-backed, persistently cached.** One-shot Claude Haiku call with Pydantic-validated output schema. Hash of normalized raw text → cache key. ~$0.50 to parse the entire current library; near-zero steady state.

## Unit normalization (at parse time)

Unify *within a dimension*. Never cross dimensions.

| Dimension | Canonical |
|---|---|
| Volume | `ml` |
| Weight | `g` |
| Count | `ea` (display blank or "whole" — user never sees "ea") |
| Package | kept verbatim (`can`, `jar`, `bottle`, `bunch`, `head`, `box`, `bag`) |

Examples:

| Input | qty | unit | original_qty | original_unit |
|---|---|---|---|---|
| `2 tbsp olive oil` | 30 | ml | 2 | tbsp |
| `½ cup olive oil` | 118 | ml | 0.5 | cup |
| `100 g olive oil` | 100 | g | 100 | g |
| `1 sweet green pepper` | 1 | ea | 1 | (blank) |
| `2 (15oz) cans kidney beans` | 2 | can | 2 | can |

When recipe specifies a *package size*, both quantities are stored: `qty=2, unit=can, package_qty=15, package_unit=oz`.

Cross-dimension unification (e.g., olive oil in both `ml` and `g`) is **not** done. Two separate grocery lines if both appear.

## Post-parse rules (applied after strangetom)

A small set of deterministic rules run after strangetom returns, before the row is persisted. Each catches a class of parse outputs that strangetom's CRF misses or formats inconsistently. All live in `mise/services/ingredient_parser.py` and `mise/services/xlsx_importer.py`; bump `PARSER_VERSION` when adding rules so `ParserCache` invalidates.

### Container-size fallback (`_CONTAINER_SIZE_RE`)

Regex catches `N (oz|ounce|ounces|g|grams|ml|l|...) (can|cans|jar|jars|bottle|bottles|box|boxes|bag|bags|pkg|pkgs|package|packages)` patterns and stashes the size in `size_descriptor`. Handles dotted abbreviations, hyphens, and parenthesized forms.

| Source | Captured size_descriptor |
|---|---|
| `1 14.5 oz can diced tomatoes` | `14.5 oz` |
| `1 (15-oz.) can refried black beans` | `15 oz` |
| `1 28-ounce can crushed tomatoes` | `28 oz` |
| `2 16 oz bottles of beer` | `16 oz` |

Unit normalized to short form (`ounce → oz`, `grams → g`, `liter → l`). Only fires when strangetom didn't already extract a size — no override of confident parses.

### Unit-word stripping (`_strip_unit_word_from_name`)

Strips whole-word matches of the unit (and its synonyms/plurals) from `display_name` at write time, so `garlic cloves` with `unit=clove` renders as just `garlic`. Applied in both the xlsx importer and the reprocess endpoint.

Synonym groups in `_UNIT_WORD_GROUPS` cover plurals (`clove/cloves`), abbreviations (`tbsp/tablespoon/tablespoons`), and — for container units only — the past-participle adjective form (`can/cans/canned`, `jar/jars/jarred`, etc.). Adjective forms are deliberately omitted for cuts/tools (`slice/sliced` would shave legitimate prep notes).

Examples (backfilled 20 rows when introduced):

| display before | unit | display after |
|---|---|---|
| `garlic cloves` | `clove` | `garlic` |
| `celery stalks` | `stalk` | `celery` |
| `can garbanzo beans` | `can` | `garbanzo beans` |
| `canned tomatoes` | `can` | `tomatoes` |
| `sliced almonds` | `slice` | `sliced almonds` (unchanged — sliced is prep, not redundancy) |

Empty result after stripping → falls back to original name (no silently empty rows).

## Canonical names

**Specific over general.** Yellow onion ≠ red onion ≠ sweet onion.

```python
class CanonicalNameResolver:
    def resolve(self, display_name: str) -> str:
        # 1. Direct lookup in alias table (deterministic)
        if hit := self.aliases.get(normalize(display_name)):
            return hit
        # 2. Fuzzy match against known canonicals (deterministic, ≥0.95)
        if match := self.fuzzy_match(display_name):
            self._record_alias(display_name, match, source="fuzzy")
            return match
        # 3. LLM resolution (slow, cached)
        canonical = self.llm_resolve(display_name)
        self._record_alias(display_name, canonical, source="llm")
        return canonical
```

The alias table (`IngredientAlias`) grows over time. Manual overrides flip `source` to `"manual"`. A `/admin/canonical-review` page surfaces low-confidence assignments.

### Examples of the canonical line

| Two phrasings | Same canonical? |
|---|---|
| "yellow onion" / "yellow onions" | ✓ (pluralization noise) |
| "yellow onion" / "Spanish onion" | ✓ (alias) |
| "yellow onion" / "red onion" | ✗ (different varieties) |
| "yellow onion" / "sweet onion" | ✗ |
| "garlic" / "garlic clove" | ✓ (clove is the unit) |
| "chicken thigh" / "boneless skinless chicken thigh" | judgment call — default to ✗ if the user wrote BSL; keep specific |
| "olive oil" / "extra virgin olive oil" | ✗ if specified; ✓ if both are bare "olive oil" |
| "milk" / "whole milk" / "2% milk" | ✗ |

**Bias toward splitting when in doubt.** Two grocery lines is annoying; one wrong grocery line is worse.

## Aliases & seeds

Ship a `seeds/canonical_aliases.json` with ~200 obvious entries (pluralization rules, common synonyms like "spring onion" = "green_onion"). Loaded into the alias table on first boot.

## Substitution graph (separate from identity)

Identity stays narrow; substitution is a sibling concept.

```python
class IngredientSubstitution(SQLModel, table=True):
    from_canonical: str           # "yellow_onion"
    to_canonical: str             # "white_onion"
    bidirectional: bool = True
    confidence: float             # 1.0 = same dish, 0.7 = noticeable
    context: str | None           # "cooked dishes only"
```

Used by suggester / pantry checks: "recipe wants yellow_onion, you have white_onion + 0.9 confidence → eligible." Never collapses identity.

Not built in v1; data model only.

## Aggregation (the read side)

The grocery builder reads parsed ingredient rows and aggregates by `(canonical_name, unit)`. With package info, it gets smarter:

```
Recipe A: 2 (15oz) cans crushed tomatoes  →  qty=2,  unit=can, pkg=15oz
Recipe B: 1 (28oz) can crushed tomatoes   →  qty=1,  unit=can, pkg=28oz
Recipe C: 14 oz crushed tomatoes          →  qty=14, unit=oz,  pkg=null

1. Convert to base volume/weight using package size:
   A: 2 × 15oz = 30 oz
   B: 1 × 28oz = 28 oz
   C: 14 oz    = 14 oz
   Total: 72 oz

2. Available packages (from seen-it-before catalog): {14oz, 15oz, 28oz}

3. Suggest: "5 × 15oz cans (75 oz)" or "2 × 28oz + 1 × 15oz (71 oz)"
```

Behind a `prefer_packages: bool` flag on the grocery builder, default true. When no package info anywhere, fall back to dumb sum-of-canonical-unit.

## Failure modes

| Failure | Response |
|---|---|
| Parser returns invalid JSON | Save row with `qty/unit` null, populate `raw_text`, flag in SyncRun |
| Quantity unparseable ("to taste") | `optional=true`, `qty=0`, `unit=null` |
| Unit unrecognized | Treat as canonical text; surface for manual normalization |
| Canonical name resolution fails | Use lowercased-underscored display_name; surface for review |

Never silent failure. The system gets a row in the DB *and* a flag for human attention.
