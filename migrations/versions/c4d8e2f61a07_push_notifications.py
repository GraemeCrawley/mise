"""push subscriptions + push event trail

Revision ID: c4d8e2f61a07
Revises: a7b2d4e91c35
Create Date: 2026-11-15 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c4d8e2f61a07'
down_revision: Union[str, Sequence[str], None] = 'a7b2d4e91c35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'pushsubscription',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('endpoint', sa.String(), nullable=False),
        sa.Column('p256dh', sa.String(), nullable=False),
        sa.Column('auth', sa.String(), nullable=False),
        sa.Column('label', sa.String(), nullable=True),
        sa.Column('origin', sa.String(), nullable=True),
        sa.Column('user_agent', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_ok_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('endpoint'),
    )
    op.create_table(
        'pushevent',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('test_id', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('subscription_id', sa.Integer(), nullable=True),
        sa.Column('stage', sa.String(), nullable=False),
        sa.Column('detail', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_pushevent_test_id', 'pushevent', ['test_id'])
    op.create_index('ix_pushevent_subscription_id', 'pushevent', ['subscription_id'])
    op.create_index('ix_pushevent_created_at', 'pushevent', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_pushevent_created_at', table_name='pushevent')
    op.drop_index('ix_pushevent_subscription_id', table_name='pushevent')
    op.drop_index('ix_pushevent_test_id', table_name='pushevent')
    op.drop_table('pushevent')
    op.drop_table('pushsubscription')
