"""recipe votes: each phone's top 4 for the week being planned

Revision ID: b7d2f4a9c113
Revises: e9a1c3b5d702
Create Date: 2026-11-16 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b7d2f4a9c113'
down_revision: Union[str, Sequence[str], None] = 'e9a1c3b5d702'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'recipevote',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('week', sa.Date(), nullable=False),
        sa.Column('device', sa.String(), nullable=False),
        sa.Column('recipe_id', sa.Integer(), sa.ForeignKey('recipe.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('week', 'device', 'recipe_id', name='uq_vote_per_phone'),
    )
    op.create_index('ix_recipevote_week', 'recipevote', ['week'])
    op.create_index('ix_recipevote_device', 'recipevote', ['device'])


def downgrade() -> None:
    op.drop_index('ix_recipevote_device', 'recipevote')
    op.drop_index('ix_recipevote_week', 'recipevote')
    op.drop_table('recipevote')
