"""ingredient edited flag

Revision ID: f3a1c9d47b21
Revises: c2d5e8f63b9a
Create Date: 2026-07-14 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f3a1c9d47b21'
down_revision: Union[str, Sequence[str], None] = 'c2d5e8f63b9a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('ingredient', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('edited', sa.Boolean(), nullable=False,
                      server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table('ingredient', schema=None) as batch_op:
        batch_op.drop_column('edited')
