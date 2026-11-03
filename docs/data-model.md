# Data model

SQLite via SQLModel. All tables scoped by `household_id` (always 1 in v1) for forward compatibility.

## Entity overview

```
Household ─┬─ Recipe ─┬─ IngredientSection ─ Ingredient
           │          └─ StepSection ─ Step
           │
           ├─ CookingSession ─ SlotAssignment
           ├─ Template ─ TemplateEntry
           ├─ PantryItem
           └─ (operational tables: SyncRun, ParserCache, RecipeCorrection, IngredientAlias)
```

## Tables

### `household`
```python
class Household(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    name: str = "Home"
    timezone: str = "UTC"
    default_servings_per_meal: int = 2
    grocery_categories: list[str] = Field(sa_column=Column(JSON))
    # default: ["Produce", "Dairy", "Meat", "Bakery", "Deli",
    #          "Frozen", "Pantry", "Sauces", "Pasta/Rice",
    #          "Snacks", "Beverages", "Spices", "Other"]
    created_at: datetime
```

One row. Seeded at first boot.

### `recipe`
```python
class Recipe(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    household_id: int = Field(foreign_key="household.id")

    # naming (matches the legacy sheet structure)
    name_original: str       # native script ("عرايس", "饺子")
    name_romanized: str | None  # transliteration ("Arayes", "Jiǎozi")
    name_english: str | None    # English title
    language: str | None

    # core
    servings: int
    prep_minutes: int | None
    cook_minutes: int | None
    total_minutes: int | None       # may be derived or overridden
    oven_temp_f: int | None
    emoji: str | None

    # tags + meta
    suggested_meals: list[str] = Field(sa_column=Column(JSON))  # ["lunch","dinner"]
    tags: list[str] = Field(sa_column=Column(JSON), default_factory=list)
    source_url: str | None
    notes_markdown: str | None

    # lifecycle
    archived: bool = False
    archived_reason: str | None
    created_at: datetime
    updated_at: datetime

    # relationships
    ingredient_sections: list["IngredientSection"]
    step_sections: list["StepSection"]
    do_ahead_tasks: list["DoAheadTask"]
```

### `ingredient_section` / `ingredient`
```python
class IngredientSection(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    recipe_id: int = Field(foreign_key="recipe.id")
    name: str | None       # null for default/single-section recipes
    order: int

class Ingredient(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    section_id: int = Field(foreign_key="ingredientsection.id")
    order: int

    # display
    raw_text: str                         # original input as written
    display_name: str                     # "yellow onion", "olive oil"
    prep_note: str | None                 # "diced", "to taste"
    size_descriptor: str | None           # "large", "medium"

    # aggregation key
    canonical_name: str = Field(index=True)   # "yellow_onion"

    # canonical quantity (math)
    qty: float
    unit: str                             # canonical: ml, g, ea, can, …

    # original (audit / display fallback)
    original_qty: float | None
    original_unit: str | None

    # packaging (when recipe specifies it)
    package_qty: float | None             # 15
    package_unit: str | None              # "oz"

    category: str                         # grocery section
    optional: bool = False
```

### `step_section` / `step`
```python
class StepSection(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    recipe_id: int = Field(foreign_key="recipe.id")
    name: str | None
    order: int

class Step(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    section_id: int = Field(foreign_key="stepsection.id")
    order: int                # 1-indexed within section
    text: str
```

### `do_ahead_task`
```python
class DoAheadTask(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    recipe_id: int = Field(foreign_key="recipe.id")
    lead_time: str            # enum: "30min", "1h", "2h", "4h", "8h", "night_before", "morning_of", "day_before", "2_days_before"
    task_type: str            # enum: "thaw", "marinate", "soak", "brine", "proof", "chill", "bring_to_room_temp", "prep_veg", ...
    note: str | None
    order: int

# Enum vocabularies deferred until we see the real recipes.
```

### `cooking_session`
```python
class CookingSession(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    household_id: int = Field(foreign_key="household.id")
    recipe_id: int = Field(foreign_key="recipe.id")

    cook_date: date           # absolute; no plan grouping
    cook_meal: str            # "breakfast"/"lunch"/"dinner"

    batch_multiplier: float = 1.0
    servings_per_slot: int = 2     # overrides household default
    notes: str | None

    cooked_at: datetime | None     # null = not yet (manual mark for v1)
    created_at: datetime
    updated_at: datetime

    slots: list["SlotAssignment"]
```

### `slot_assignment`
```python
class SlotAssignment(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    session_id: int = Field(foreign_key="cookingsession.id")
    date: date                # absolute
    meal: str
    position: int             # 1 = cook, 2+ = leftovers
    skipped: bool = False
    consumed_at: datetime | None

    __table_args__ = (
        UniqueConstraint("date", "meal", name="uq_slot_per_meal"),
        # ↑ enforces "one thing planned per (date, meal)"
    )
```

### `template` / `template_entry`
```python
class Template(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    household_id: int
    name: str
    notes: str | None
    created_at: datetime

class TemplateEntry(SQLModel, table=True):
    template_id: int = Field(foreign_key="template.id")
    day_offset: int           # 0 = first day of applied range
    meal: str
    recipe_id: int
    batch_multiplier: float = 1.0
    servings_per_slot: int = 2
```

### `pantry_item`
```python
class PantryItem(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    household_id: int
    canonical_name: str = Field(index=True)
    qty: float
    unit: str
    location: str | None      # "pantry"|"fridge"|"freezer" (informational)
    expires_at: date | None
    notes: str | None
    added_at: datetime
    updated_at: datetime
```

In v1, pantry rows are created/updated mostly through the grocery flow ("got it" / "partial").

### Operational tables

```python
class SyncRun(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    started_at: datetime
    finished_at: datetime | None
    status: str               # "success"|"partial"|"failed"
    counts: dict = Field(sa_column=Column(JSON))
    needs_review: list[str] = Field(sa_column=Column(JSON))
    error_log: str | None

class SyncState(SQLModel, table=True):
    recipe_id: str = Field(primary_key=True)   # sheet UUID
    sheet_row_hash: str
    last_seen_at: datetime
    last_synced_at: datetime

class ParserCache(SQLModel, table=True):
    raw_text_hash: str = Field(primary_key=True)
    raw_text: str
    result_json: str
    parser_version: str
    created_at: datetime

class IngredientAlias(SQLModel, table=True):
    display_name: str = Field(primary_key=True)
    canonical_name: str = Field(index=True)
    confidence: float
    source: str               # "manual"|"llm"|"seed"
    created_at: datetime

class RecipeCorrection(SQLModel, table=True):
    id: int | None = Field(primary_key=True)
    recipe_id: int
    field: str                # "servings", "ingredient.qty", ...
    old_value: str | None
    new_value: str | None
    reason: str | None
    source: str               # "user"|"sync"|"agent"
    created_at: datetime
```

## Indexes

- `recipe.archived` for filtering library views
- `ingredient.canonical_name` for aggregation
- `slot_assignment.date` + composite `(date, meal)` (already a unique constraint)
- `pantry_item.canonical_name` for grocery deduction lookup
- FTS5 virtual table mirroring `recipe.name_*` + `tags` + `notes_markdown` for search

## Soft delete

`archived = true` instead of DELETE. Sync re-appearance un-archives. Audit row in `RecipeCorrection`.

## Migration patterns

- Alembic from day one
- One schema-changing PR at a time, never combined with logic changes
- Data backfills as separate Alembic data migrations or one-shot scripts in `scripts/`
- `parser_version` enables LLM re-parses without schema change (bump version, invalidate cache, batch re-run)
