"""Non-destructive migration to natural-unit storage.

Rewrites rows that are provably unchanged parser output; preserves (and
flags) anything a human may have edited. Safe to re-run (idempotent).

Usage:
    uv run python scripts/migrate_natural_units.py            # dry-run
    uv run python scripts/migrate_natural_units.py --apply
"""
from __future__ import annotations

import sys

from pint import UnitRegistry
from sqlmodel import Session, select

from mise.db import engine
from mise.models import Ingredient
from mise.services.ingredient_parser import _abbrev_unit

_ureg = UnitRegistry()


def plan_row(ing: Ingredient) -> tuple:
    """Decide what to do with one row. Pure; no DB.

    Returns ("overwrite", qty, unit) or ("preserve", reason).
    """
    if ing.edited:
        return ("preserve", "edited-flag")
    if ing.original_unit is None:
        # No conversion was ever recorded -> stored unit is already natural
        # (count/package, or input already canonical). Leave it.
        return ("preserve", "already-natural")
    # A conversion was recorded. Verify the stored canonical value is what
    # the OLD parser would have produced from the original; if not, a human
    # changed it (pre-flag edit) -> preserve.
    try:
        expected = (ing.original_qty * _ureg(ing.original_unit)).to(ing.unit).magnitude
    except Exception:
        return ("preserve", "unconvertible")
    if abs(expected - ing.qty) > max(1.0, 0.02 * expected):
        return ("preserve", "diverged")
    natural_unit = _abbrev_unit(ing.original_unit) or ing.original_unit
    return ("overwrite", ing.original_qty, natural_unit)


def main(apply: bool) -> None:
    overwrites = preserves = flagged = 0
    with Session(engine()) as db:
        for ing in db.exec(select(Ingredient)).all():
            plan = plan_row(ing)
            if plan[0] == "overwrite":
                overwrites += 1
                if apply:
                    ing.qty, ing.unit = plan[1], plan[2]
                    ing.original_qty = ing.original_unit = None
                    db.add(ing)
            else:
                preserves += 1
                if plan[1] == "diverged" and apply and not ing.edited:
                    ing.edited = True  # lock in detected pre-flag edits
                    db.add(ing)
                    flagged += 1
        if apply:
            db.commit()
    print(f"overwrite={overwrites} preserve={preserves} newly-flagged={flagged} "
          f"({'APPLIED' if apply else 'dry-run'})")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
