#!/usr/bin/env python3
"""One-shot backfill: re-apply apply_category_preference() to every
existing Ingredient row in the DB. Uses the preserved original_qty +
original_unit fields, so no re-parsing required.

Idempotent — running it twice produces the same result.

Run inside the container:
  docker exec mise uv run python /app/scripts/backfill_preferred_units.py
"""

from __future__ import annotations

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select

from mise.db import engine
from mise.models import Ingredient
from mise.services.ingredient_parser import apply_category_preference


def main() -> int:
    changed: Counter[str] = Counter()
    with Session(engine()) as db:
        ings = db.exec(select(Ingredient)).all()
        for ing in ings:
            before = (ing.qty, ing.unit, ing.original_qty, ing.original_unit)
            qty, unit, oq, ou = apply_category_preference(
                ing.qty, ing.unit, ing.original_qty, ing.original_unit, ing.category
            )
            if (qty, unit, oq, ou) != before:
                ing.qty, ing.unit, ing.original_qty, ing.original_unit = qty, unit, oq, ou
                db.add(ing)
                changed[ing.category] += 1
        db.commit()

    total = sum(changed.values())
    print(f"updated {total} rows")
    for cat, n in sorted(changed.items(), key=lambda x: -x[1]):
        print(f"  {cat:12} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
