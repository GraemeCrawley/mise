"""Canonical-name resolver.

Maps any input name (strangetom's `foundation_food` text, the raw
display_name from a recipe form, a user override) to a `canonical_name`
— the snake_case key used by the grocery aggregator to combine
ingredients across recipes.

Phase 0 cut: deterministic only.
  1. Lookup the lowercased input in `IngredientAlias` (PK).
  2. Miss → snake-case the input as a deterministic default.
  3. Insert the result so future calls hit the cache.

Followup commit will add an LLM verify layer that runs on first-seen
auto-generated aliases and either confirms (source="auto") or rewrites
(source="llm") the canonical_name. The IngredientAlias schema already
carries `source` and `confidence` for that distinction; no migration
needed.

Per docs/ingredient-parsing.md, canonical names should be specific:
yellow_onion ≠ red_onion. The snake-case default preserves whatever
specificity the input already had — "Beef, steak, chuck" stays
"beef_steak_chuck" (no auto-collapse to just "beef").
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from mise.models import IngredientAlias

log = logging.getLogger("mise.canonical")


def snake_case(name: str) -> str:
    """Deterministic canonical baseline.

      'Beef, steak, chuck'    → 'beef_steak_chuck'
      'Yellow Onion'          → 'yellow_onion'
      'Olive oil'             → 'olive_oil'
      'Salt, table, iodized'  → 'salt_table_iodized'
      'Extra virgin olive oil'→ 'extra_virgin_olive_oil'
    """
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return s or "unknown"


def resolve(
    name: str,
    db: Session,
) -> tuple[str, float, str]:
    """Resolve `name` to `(canonical_name, confidence, source)`.

    Cached forever in IngredientAlias on first encounter (auto-generated
    rows get source="auto"; LLM verify can later overwrite with
    source="llm"; user edits flip to "manual").

    Empty / missing names resolve to ("unknown", 0.0, "default") without
    a cache write — caller decides whether to flag needs_review.
    """
    if not name or name.strip().lower() in {"", "unknown"}:
        return "unknown", 0.0, "default"

    key = name.lower().strip()

    # 1. Existing alias?
    alias = db.get(IngredientAlias, key)
    if alias is not None:
        return alias.canonical_name, alias.confidence, alias.source

    # 2. Snake-case the input as the deterministic default
    canonical = snake_case(name)

    # 3. Cache. The unique-by-display-name constraint is the PK, so
    # parallel inserts race-loss into IntegrityError — recoverable by
    # re-reading.
    #
    # NOTE: we flush() not commit() so resolve() participates in
    # whatever transaction the caller has open. Transaction management
    # is the caller's responsibility (e.g. xlsx_importer manages
    # savepoints around each row).
    try:
        db.add(
            IngredientAlias(
                display_name=key,
                canonical_name=canonical,
                confidence=0.5,  # low — auto-generated; verify can override
                source="auto",
            )
        )
        db.flush()
    except IntegrityError:
        # Someone else inserted within the current transaction —
        # re-read theirs. The IntegrityError marks the savepoint
        # invalid so the caller's per-row savepoint will roll back
        # cleanly; here we just read the canonical and continue.
        alias = db.get(IngredientAlias, key)
        if alias is not None:
            return alias.canonical_name, alias.confidence, alias.source
        log.warning("canonical resolve: cache write race on %r", key)

    return canonical, 0.5, "auto"
