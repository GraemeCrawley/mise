"""plan note (free-text plan entry)

Revision ID: a7b2d4e91c35
Revises: f3a1c9d47b21
Create Date: 2026-10-12 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7b2d4e91c35'
down_revision: Union[str, Sequence[str], None] = 'f3a1c9d47b21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'plannote',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('household_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('meal', sa.String(), nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['household.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('date', 'meal', name='uq_note_per_meal'),
    )
    op.create_index('ix_plannote_date', 'plannote', ['date'])


def downgrade() -> None:
    op.drop_index('ix_plannote_date', table_name='plannote')
    op.drop_table('plannote')
