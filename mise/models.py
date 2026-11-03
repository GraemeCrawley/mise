"""SQLModel definitions. One file per docs/architecture.md ("dumb data
containers, no behaviour"). Splits possible later if file growth
warrants — for 17 small tables a single file is easier to scan.

Schema is the source of truth defined in docs/data-model.md. Any change
here MUST come with an Alembic migration. Never call
`SQLModel.metadata.create_all()` outside of tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

# Alias the `date` type — SlotAssignment.date (field) would otherwise
# clash with the type in its annotation. Pydantic refuses that.
from datetime import date as _Date

from sqlalchemy import JSON, Column, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field, SQLModel

date = _Date  # re-export for the non-clashing call sites

# --- household ------------------------------------------------------------


class Household(SQLModel, table=True):
    """Single-tenant in v1; the `household_id` column on every other
    table is for forward-compatibility."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = "Home"
    timezone: str = "UTC"
    default_servings_per_meal: int = 2
    # Default order matches docs/data-model.md §household. Editable.
    grocery_categories: list[str] = Field(
        sa_column=Column(JSON),
        default_factory=lambda: [
            "Produce", "Herbs", "Dairy", "Meat", "Bakery", "Deli",
            "Frozen", "Pantry", "Sauces", "Pasta/Rice",
            "Snacks", "Beverages", "Spices", "Other",
        ],
    )
    # Meal slots per day, in display order. Same pattern as
    # grocery_categories — forward-compat for adding "snack" later
    # without a code change; the planner UI iterates over this list.
    meals: list[str] = Field(
        sa_column=Column(JSON),
        default_factory=lambda: ["breakfast", "lunch", "dinner"],
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --- recipe + ingredients + steps + do-ahead ------------------------------


class Recipe(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    household_id: int = Field(foreign_key="household.id", index=True)

    # naming (legacy sheet preserves all three name variants)
    name_original: str
    name_romanized: str | None = None
    name_english: str | None = None
    language: str | None = None

    # core
    servings: int
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    total_minutes: int | None = None
    oven_temp_f: int | None = None
    emoji: str | None = None

    # tags + meta
    suggested_meals: list[str] = Field(sa_column=Column(JSON), default_factory=list)
    tags: list[str] = Field(sa_column=Column(JSON), default_factory=list)
    source_url: str | None = None
    notes_markdown: str | None = None

    # lifecycle (soft delete; re-import un-archives if the row hash matches)
    archived: bool = Field(default=False, index=True)
    archived_reason: str | None = None
    # Set when the operator marks the recipe "complete" on the review
    # surface. Cleared on re-import so the row re-enters the queue if
    # the source changed.
    reviewed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Set when this recipe came from an xlsx import. sha256 of the
    # normalised row contents; used for idempotent re-imports.
    # Manually-created recipes (Phase 1 form) leave this NULL.
    source_row_hash: str | None = Field(default=None, index=True)

    def display_title(self) -> str:
        """Best name for UI lists / cards / planner slots.

        Prefers name_english by schema convention. Falls back to
        name_original ONLY when name_english is mostly non-Latin
        script (Arabic, CJK, Cyrillic) — the field name is a bit
        misleading, it really just means "primary", and some recipes
        store the Arabic / Latin pair the "wrong" way around.

        Algorithm: strip parens content first (often contains a
        romanization hint like "म-ma ट-ta र-ra" that would otherwise
        inflate the Latin fraction past the threshold), then compute
        Latin fraction of the remaining letters. ≥50% Latin → keep
        name_english; else fall back to name_original.

        Corner cases this handles correctly:
          - "Mulled Wine" / "Conditum Paradoxum" → keep name_english
            (no parens; raw fraction is 100% Latin)
          - "عرايس (عر-Arr ايس-Aise)" / "Arayes (Meat Brides)" →
            fall back (strip parens → "عرايس" → 0% Latin)
          - "मटर पनीर (म-ma ट-ta र-ra ...)" / "Matar Paneer" →
            fall back (strip parens → "मटर पनीर" → 0% Latin)
          - "饺子 (饺-jiǎo 子-zi)" / "Dumplings" →
            fall back (strip parens → "饺子" → 0% Latin)
        """
        import re as _re
        import unicodedata as _ud

        def _latin_fraction(s: str | None) -> float:
            stripped = _re.sub(r"\([^)]*\)", "", s or "")
            letters = [c for c in stripped if _ud.category(c).startswith("L")]
            if not letters:
                return 1.0
            latin = sum(1 for c in letters if "a" <= c.lower() <= "z")
            return latin / len(letters)

        if self.name_english and _latin_fraction(self.name_english) >= 0.5:
            return self.name_english
        return self.name_original or self.name_english or "(unnamed)"



class IngredientSection(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    # ondelete=CASCADE: when a Recipe is deleted, its sections (and
    # via further cascades, all ingredients) go too — no orphan rows.
    recipe_id: int = Field(
        sa_column=Column(
            Integer, ForeignKey("recipe.id", ondelete="CASCADE"), nullable=False
        )
    )
    name: str | None = None  # null for default/single-section recipes
    order: int



class Ingredient(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    section_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("ingredientsection.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    order: int

    # display
    raw_text: str
    display_name: str
    prep_note: str | None = None
    size_descriptor: str | None = None
    # Verbatim line from the source xlsx, before any parsing — the
    # absolute origin of what eventually became this row. Useful for
    # the preview's "compare with source" sub-line + future re-parse
    # without re-reading the xlsx.
    source_line: str | None = None
    # LLM-extracted alternative from the source line ("or beef stock",
    # "or 1 tsp dried thyme"). Surfaces in the display next to the
    # main name. Per-row; not cached on IngredientAlias.
    alternatives_note: str | None = None

    # aggregation key
    canonical_name: str = Field(index=True)

    # canonical quantity (math)
    qty: float
    unit: str  # canonical: ml, g, ea, can, …

    # original (audit / display fallback)
    original_qty: float | None = None
    original_unit: str | None = None

    # packaging (when recipe specifies it; "1 can = 15 oz")
    package_qty: float | None = None
    package_unit: str | None = None

    category: str  # grocery section, FK-by-string to household.grocery_categories
    optional: bool = False
    # True once a human corrects qty/unit/display_name/optional via the
    # edit endpoint. The natural-unit migrator preserves flagged rows.
    edited: bool = False



class StepSection(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    recipe_id: int = Field(
        sa_column=Column(
            Integer, ForeignKey("recipe.id", ondelete="CASCADE"), nullable=False
        )
    )
    name: str | None = None
    order: int



class Step(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    section_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("stepsection.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    order: int  # 1-indexed within section
    text: str



class DoAheadTask(SQLModel, table=True):
    """Enum vocabularies (lead_time, task_type) deliberately deferred
    until the real recipe corpus exposes the patterns. Stored as plain
    str for now; a constrained enum is a 1-line schema migration when
    we know the shape."""

    id: int | None = Field(default=None, primary_key=True)
    recipe_id: int = Field(foreign_key="recipe.id")
    lead_time: str
    task_type: str
    note: str | None = None
    order: int



# --- cooking session + slot assignment ------------------------------------


class CookingSession(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    household_id: int = Field(foreign_key="household.id", index=True)
    recipe_id: int = Field(foreign_key="recipe.id")

    cook_date: date
    cook_meal: str  # "breakfast"|"lunch"|"dinner"

    batch_multiplier: float = 1.0
    servings_per_slot: int = 2
    notes: str | None = None

    cooked_at: datetime | None = None  # v1: manual mark
    # When set, this session's ingredients no longer aggregate into
    # the shop list — the user marked "I've already shopped for this".
    # Clearable via the unshop endpoint.
    shopped_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))



class SlotAssignment(SQLModel, table=True):
    __table_args__ = (
        # Enforces "one thing planned per (date, meal)" — see spec §planner.
        UniqueConstraint("date", "meal", name="uq_slot_per_meal"),
    )

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="cookingsession.id", index=True)
    date: _Date = Field(index=True)
    meal: str
    position: int  # 1 = cook, 2+ = leftovers
    skipped: bool = False
    consumed_at: datetime | None = None



class PlanNote(SQLModel, table=True):
    """Free-text plan entry ("Leftover Thanksgiving dinner", "Eating out").
    Occupies its (date, meal) like a SlotAssignment but has no recipe, so
    it never reaches the shopping list."""
    __table_args__ = (
        UniqueConstraint("date", "meal", name="uq_note_per_meal"),
    )

    id: int | None = Field(default=None, primary_key=True)
    household_id: int = Field(foreign_key="household.id")
    date: _Date = Field(index=True)
    meal: str
    text: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --- template -------------------------------------------------------------


class Template(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    household_id: int = Field(foreign_key="household.id")
    name: str
    notes: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))



class TemplateEntry(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    template_id: int = Field(foreign_key="template.id")
    day_offset: int  # 0 = first day of applied range
    meal: str
    recipe_id: int = Field(foreign_key="recipe.id")
    batch_multiplier: float = 1.0
    servings_per_slot: int = 2



# --- pantry ---------------------------------------------------------------


class PantryItem(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    household_id: int = Field(foreign_key="household.id")
    canonical_name: str = Field(index=True)
    qty: float
    unit: str
    location: str | None = None  # "pantry"|"fridge"|"freezer"
    expires_at: date | None = None
    notes: str | None = None
    added_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --- operational (sync, parser cache, aliases, corrections) ---------------


class SyncRun(SQLModel, table=True):
    """One row per import attempt (xlsx import in v1; future URL imports
    would also write here). Per-recipe identity is tracked on the Recipe
    row itself via `source_row_hash` — no separate SyncState table."""

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    status: str  # "success"|"partial"|"failed"|"running"
    source: str = "xlsx"  # "xlsx"|"url"|"manual-bulk"|...
    counts: dict = Field(sa_column=Column(JSON), default_factory=dict)
    needs_review: list[str] = Field(sa_column=Column(JSON), default_factory=list)
    error_log: str | None = None


class ParserCache(SQLModel, table=True):
    """Hash-keyed cache for the ingredient parser. `parser_version` bump
    is the effective invalidation — old rows stay (audit) but writes go
    to new keys."""

    raw_text_hash: str = Field(primary_key=True)
    raw_text: str
    result_json: str
    parser_version: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IngredientAlias(SQLModel, table=True):
    display_name: str = Field(primary_key=True)
    canonical_name: str = Field(index=True)
    # Category override stored alongside the canonical-name mapping —
    # both come from the same LLM verify pass for first-seen foundation
    # foods. NULL when source="auto" (deterministic fallback didn't
    # decide a category; importer falls through to the prefix map).
    category: str | None = None
    # Human-friendly display name (LLM-humanised from the FDC-flavoured
    # PK). NULL until humanise pass runs; importer falls back to PK.
    display_name_pretty: str | None = None
    confidence: float
    source: str  # "auto"|"manual"|"llm"|"seed"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RecipeCorrection(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    recipe_id: int = Field(foreign_key="recipe.id", index=True)
    field: str  # "servings", "ingredient.qty", ...
    old_value: str | None = None
    new_value: str | None = None
    reason: str | None = None
    source: str  # "user"|"sync"|"agent"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ShopExtra(SQLModel, table=True):
    """Ad-hoc shopping list items — things added directly at the store,
    not aggregated from planned recipes. Belong to a household so they
    survive across devices (replacing the localStorage stopgap that
    was on /preview/shopping). list_key scopes them to a particular
    list view ('week' for the current week's shop, or a hash of an
    explicit recipe-id set)."""

    id: int | None = Field(default=None, primary_key=True)
    household_id: int = Field(foreign_key="household.id", index=True)
    list_key: str = Field(index=True, default="week")
    name: str  # free-text; user enters whatever they want
    qty: str | None = None  # display string e.g. "2", "1 dozen"
    category: str = "Other"  # for section grouping
    checked: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ShopCheck(SQLModel, table=True):
    """Household-shared check-off state for aggregated shopping-list
    items. Aggregated items are derived from the week's planned recipes
    (not stored anywhere) — we identify them by the same key the UI
    uses (canonical_name + unit). When one cook ticks an item, the row
    here lets the other cook's device see the same checked state
    after a refresh / when the page reopens.

    list_key scopes to a list view ('week' for the current week's
    shop). Presence of a row = checked; absence = unchecked. We delete
    on un-check rather than carrying a boolean column so the table
    stays small (only checked items live here)."""

    id: int | None = Field(default=None, primary_key=True)
    household_id: int = Field(foreign_key="household.id", index=True)
    list_key: str = Field(index=True, default="week")
    item_key: str = Field(index=True)  # canonical-name|unit from the aggregator
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Feedback(SQLModel, table=True):
    """In-app feedback captured via the floating button on every preview page.
    The whole point is the feedback loop — the household leaves notes mid-use, we
    triage them from /preview/feedback. Kind is a coarse bucket so we can
    sort by intent without forcing a schema."""

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    path: str  # the page they were on, e.g. "/preview/plan"
    kind: str = "note"  # "idea" | "bug" | "like" | "note"
    content: str
    who: str | None = None  # optional name they typed (no auth yet)
    status: str = "new"  # "new" | "reviewed" | "done" | "wontfix"
    # Optional screenshot of the page state at submission time. Path is
    # relative to a screenshots directory (resolved at serve time).
    # Deleted on status transition out of "new" so the disk stays bounded.
    screenshot_path: str | None = None
