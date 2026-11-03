"""dedup the duplicate FKs left by the cascade migration

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-02 00:00:00.000000

The previous migration (c3d4e5f6a7b8) added cascade FKs via Alembic
batch mode but didn't drop the original no-action FKs — each affected
table ended up with two FKs on the same column. Functionally correct
(SQLite honors the CASCADE one), but cosmetically ugly and confusing
for future migrations.

This migration uses raw SQL to fully recreate each table with exactly
one FK (the cascade one). SQLite-specific. Safe because the only thing
changing is the FK constraint set — column shape, data, and indexes
are preserved.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table_name, CREATE statement, explicit column list for safe INSERT).
# The column list is essential — later migrations appended `source_line`
# and `alternatives_note` to `ingredient` at the end of the physical
# schema, so `SELECT *` would put them in the wrong slots in the
# rebuilt table. Listing columns explicitly keeps the copy correct.
_REBUILDS = [
    (
        "ingredientsection",
        """
        CREATE TABLE ingredientsection_new (
            id INTEGER NOT NULL PRIMARY KEY,
            recipe_id INTEGER NOT NULL,
            name VARCHAR,
            "order" INTEGER NOT NULL,
            FOREIGN KEY (recipe_id) REFERENCES recipe (id) ON DELETE CASCADE
        )
        """,
        ["id", "recipe_id", "name", '"order"'],
    ),
    (
        "stepsection",
        """
        CREATE TABLE stepsection_new (
            id INTEGER NOT NULL PRIMARY KEY,
            recipe_id INTEGER NOT NULL,
            name VARCHAR,
            "order" INTEGER NOT NULL,
            FOREIGN KEY (recipe_id) REFERENCES recipe (id) ON DELETE CASCADE
        )
        """,
        ["id", "recipe_id", "name", '"order"'],
    ),
    (
        "ingredient",
        """
        CREATE TABLE ingredient_new (
            id INTEGER NOT NULL PRIMARY KEY,
            section_id INTEGER NOT NULL,
            "order" INTEGER NOT NULL,
            raw_text VARCHAR NOT NULL,
            display_name VARCHAR NOT NULL,
            prep_note VARCHAR,
            size_descriptor VARCHAR,
            source_line VARCHAR,
            alternatives_note VARCHAR,
            canonical_name VARCHAR NOT NULL,
            qty FLOAT NOT NULL,
            unit VARCHAR NOT NULL,
            original_qty FLOAT,
            original_unit VARCHAR,
            package_qty FLOAT,
            package_unit VARCHAR,
            category VARCHAR NOT NULL,
            optional BOOLEAN NOT NULL,
            FOREIGN KEY (section_id) REFERENCES ingredientsection (id) ON DELETE CASCADE
        )
        """,
        [
            "id", "section_id", '"order"', "raw_text", "display_name",
            "prep_note", "size_descriptor", "source_line",
            "alternatives_note", "canonical_name", "qty", "unit",
            "original_qty", "original_unit", "package_qty", "package_unit",
            "category", "optional",
        ],
    ),
    (
        "step",
        """
        CREATE TABLE step_new (
            id INTEGER NOT NULL PRIMARY KEY,
            section_id INTEGER NOT NULL,
            "order" INTEGER NOT NULL,
            text VARCHAR NOT NULL,
            FOREIGN KEY (section_id) REFERENCES stepsection (id) ON DELETE CASCADE
        )
        """,
        ["id", "section_id", '"order"', "text"],
    ),
]


def upgrade() -> None:
    conn = op.get_bind()
    # Suspend FK enforcement during the swap; CREATE_new → INSERT →
    # DROP old → RENAME new touches the constraint graph and would
    # otherwise fail.
    conn.execute(text("PRAGMA foreign_keys=OFF"))
    try:
        for table_name, create_sql, columns in _REBUILDS:
            # Idempotent: drop any stale _new table from a previous
            # half-applied run before recreating. Lets the migration
            # recover after a partial failure.
            conn.execute(text(f'DROP TABLE IF EXISTS {table_name}_new'))
            conn.execute(text(create_sql))
            col_list = ", ".join(columns)
            conn.execute(text(
                f'INSERT INTO {table_name}_new ({col_list}) '
                f'SELECT {col_list} FROM {table_name}'
            ))
            conn.execute(text(f'DROP TABLE {table_name}'))
            conn.execute(text(
                f'ALTER TABLE {table_name}_new RENAME TO {table_name}'
            ))
            # Re-create the canonical_name index on ingredient (only
            # index that survives this rebuild — others on this table
            # set are auto-created by PRIMARY KEY).
            if table_name == "ingredient":
                conn.execute(text(
                    'CREATE INDEX IF NOT EXISTS ix_ingredient_canonical_name '
                    'ON ingredient (canonical_name)'
                ))
    finally:
        conn.execute(text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    # No-op: there's no meaningful state to revert to (the previous
    # migration's duplicate-FK situation isn't worth recreating).
    pass
