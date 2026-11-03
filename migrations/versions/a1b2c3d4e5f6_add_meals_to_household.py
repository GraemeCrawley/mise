"""add meals list to household

Revision ID: a1b2c3d4e5f6
Revises: ed32e62058c1
Create Date: 2026-05-31 21:00:00.000000

"""
from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "7e3e9ac65bad"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DEFAULT = json.dumps(["breakfast", "lunch", "dinner"])


def upgrade() -> None:
    op.add_column(
        "household",
        sa.Column("meals", sa.JSON(), nullable=True),
    )
    op.execute(f"UPDATE household SET meals = '{_DEFAULT}' WHERE meals IS NULL")


def downgrade() -> None:
    op.drop_column("household", "meals")
