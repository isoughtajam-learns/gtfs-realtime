"""Scope Route.short_name uniqueness per transit_system

Revision ID: f5fbc59d4e5c
Revises: ed0365ffc3e2
Create Date: 2026-10-15 15:43:02.735959

Confirmed live: 67 of 310 distinct route_short_name values are shared by
2-4 different Bay Area agencies (plain numeric bus route codes like
"22"/"52"/"F" are common across systems). The old global uniqueness meant
ON CONFLICT (short_name) DO UPDATE silently reassigned a colliding
short_name to whichever system's fetch ran most recently, without ever
updating transit_system_id back - every other system's own same-numbered
route vanished (visible as route_short_name/long_name/color all None for
every affected trip). Safe to apply with no data loss: existing rows
already satisfy the new, less restrictive composite constraint (if a
short_name was already globally unique, it's certainly unique within each
transit_system_id too) - this only unblocks future upserts from creating
the rows that were previously getting silently overwritten.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f5fbc59d4e5c'
down_revision: Union[str, Sequence[str], None] = 'ed0365ffc3e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f('uq_short_name'), 'route', type_='unique')
    op.create_unique_constraint('uq_short_name', 'route', ['transit_system_id', 'short_name'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_short_name', 'route', type_='unique')
    op.create_unique_constraint(op.f('uq_short_name'), 'route', ['short_name'])
