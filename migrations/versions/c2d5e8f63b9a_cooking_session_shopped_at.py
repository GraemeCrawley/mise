"""cooking session shopped_at

Revision ID: c2d5e8f63b9a
Revises: b1c4f7e92a8d
Create Date: 2026-06-10 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2d5e8f63b9a'
down_revision: Union[str, Sequence[str], None] = 'b1c4f7e92a8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('cookingsession', schema=None) as batch_op:
        batch_op.add_column(sa.Column('shopped_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cookingsession', schema=None) as batch_op:
        batch_op.drop_column('shopped_at')
