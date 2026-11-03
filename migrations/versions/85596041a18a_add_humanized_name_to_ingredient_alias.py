"""add humanized_name to ingredient_alias

Revision ID: 85596041a18a
Revises: deb34820c675
Create Date: 2026-05-29 13:04:32.114532

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '85596041a18a'
down_revision: Union[str, Sequence[str], None] = 'deb34820c675'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Pretty / human-readable display name. NULL while not yet derived;
    # populated by an LLM humanise pass for first-seen aliases. The
    # importer prefers this over the FDC-flavoured raw name when present.
    op.add_column(
        "ingredientalias",
        sa.Column("display_name_pretty", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ingredientalias", "display_name_pretty")
