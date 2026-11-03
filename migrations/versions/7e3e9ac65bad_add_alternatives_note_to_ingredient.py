"""add alternatives_note to ingredient

Revision ID: 7e3e9ac65bad
Revises: ed32e62058c1
Create Date: 2026-05-29 15:08:08.344561

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '7e3e9ac65bad'
down_revision: Union[str, Sequence[str], None] = 'ed32e62058c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ingredient",
        sa.Column("alternatives_note", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ingredient", "alternatives_note")
