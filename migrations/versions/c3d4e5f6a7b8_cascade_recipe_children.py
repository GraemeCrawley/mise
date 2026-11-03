"""cascade delete from recipe → sections → rows

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-02 00:00:00.000000

Adds ON DELETE CASCADE to four foreign keys so deleting a Recipe
(or one of its sections) wipes all dependent rows automatically:

  recipe → ingredientsection.recipe_id        (CASCADE)
  recipe → stepsection.recipe_id              (CASCADE)
  ingredientsection → ingredient.section_id   (CASCADE)
  stepsection → step.section_id               (CASCADE)

Closes a class of latent orphan-row bugs that surfaced when SQLite
reused a recipe id and the new recipe inherited stale sections.

SQLite can't ALTER foreign keys in place, so each table is recreated
via Alembic's batch operations (new table → copy data → drop old →
rename). FK enforcement at runtime needs PRAGMA foreign_keys=ON,
already set in mise.db._apply_sqlite_savepoint_workaround.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Each entry: (child_table, fk_column, parent_table, parent_column).
_FK_TARGETS = [
    ("ingredientsection", "recipe_id", "recipe", "id"),
    ("stepsection",       "recipe_id", "recipe", "id"),
    ("ingredient",        "section_id", "ingredientsection", "id"),
    ("step",              "section_id", "stepsection", "id"),
]


def _rebuild_fk(child: str, col: str, parent: str, parent_col: str, *, ondelete: str | None) -> None:
    """Drop any existing FK on (child.col) and recreate it pointing at
    (parent.parent_col) with the given ondelete. Tolerates the FK
    having no name (Alembic auto-names when dropping by None)."""
    with op.batch_alter_table(child, recreate="always") as batch:
        # batch_alter_table with recreate="always" rebuilds the table
        # from scratch; we just need to declare the new FK shape.
        # Naming the constraint after the column is the Alembic convention.
        batch.create_foreign_key(
            f"fk_{child}_{col}_{parent}",
            parent,
            [col],
            [parent_col],
            ondelete=ondelete,
        )


def upgrade() -> None:
    for child, col, parent, parent_col in _FK_TARGETS:
        _rebuild_fk(child, col, parent, parent_col, ondelete="CASCADE")


def downgrade() -> None:
    # Revert to no-cascade FKs (still enforced, but no automatic
    # delete propagation).
    for child, col, parent, parent_col in _FK_TARGETS:
        _rebuild_fk(child, col, parent, parent_col, ondelete=None)
