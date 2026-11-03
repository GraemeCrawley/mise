"""add shop_check table

Revision ID: b1c4f7e92a8d
Revises: adb99da0d561
Create Date: 2026-06-08 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = 'b1c4f7e92a8d'
down_revision: Union[str, Sequence[str], None] = 'adb99da0d561'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'shopcheck',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('household_id', sa.Integer(), nullable=False),
        sa.Column('list_key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('item_key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('checked_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['household.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_shopcheck_household_id', 'shopcheck', ['household_id'])
    op.create_index('ix_shopcheck_list_key', 'shopcheck', ['list_key'])
    op.create_index('ix_shopcheck_item_key', 'shopcheck', ['item_key'])


def downgrade() -> None:
    op.drop_index('ix_shopcheck_item_key', table_name='shopcheck')
    op.drop_index('ix_shopcheck_list_key', table_name='shopcheck')
    op.drop_index('ix_shopcheck_household_id', table_name='shopcheck')
    op.drop_table('shopcheck')
