"""add source_line to ingredient

Revision ID: ed32e62058c1
Revises: 85596041a18a
Create Date: 2026-05-29 14:01:00.845432

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'ed32e62058c1'
down_revision: Union[str, Sequence[str], None] = '85596041a18a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Verbatim line from the source xlsx, threaded through the parser
    # so the preview can show the literal original input.
    op.add_column(
        "ingredient",
        sa.Column("source_line", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ingredient", "source_line")
