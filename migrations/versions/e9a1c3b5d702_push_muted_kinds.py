"""push subscription: reminder kinds this phone has switched off

Revision ID: e9a1c3b5d702
Revises: c4d8e2f61a07
Create Date: 2026-11-15 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e9a1c3b5d702'
down_revision: Union[str, Sequence[str], None] = 'c4d8e2f61a07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('pushsubscription') as b:
        b.add_column(sa.Column('muted_kinds', sa.JSON(), nullable=False, server_default='[]'))


def downgrade() -> None:
    with op.batch_alter_table('pushsubscription') as b:
        b.drop_column('muted_kinds')
