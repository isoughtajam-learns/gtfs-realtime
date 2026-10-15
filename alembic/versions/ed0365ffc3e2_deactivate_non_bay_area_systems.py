"""Deactivate non-Bay-Area systems

Revision ID: ed0365ffc3e2
Revises: c1e945793eb9
Create Date: 2026-10-15 09:22:01.206071

The app's focus is now the Bay Area specifically (see the "Setting up new
transit systems" project's own description) - MBTA (Boston) and
NY_Waterway (New York) predate that focus and aren't part of it.
Deactivated, not deleted, same precedent as Kiev/Helsinki/Provence-Alpes
elsewhere in this table - active=False keeps the row (and anything
already ingested under its transit_system_id) intact and trivially
reversible, rather than a destructive delete cascading through
Trip/Stop/Route/StopTime.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ed0365ffc3e2'
down_revision: Union[str, Sequence[str], None] = 'c1e945793eb9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NAMES = ["MBTA", "NY_Waterway"]


def upgrade() -> None:
    """Upgrade schema."""
    transit_system = sa.table(
        "transit_system", sa.column("name", sa.String), sa.column("active", sa.Boolean)
    )
    bind = op.get_bind()
    bind.execute(
        sa.update(transit_system)
        .where(transit_system.c.name.in_(_NAMES))
        .values(active=False)
    )


def downgrade() -> None:
    """Downgrade schema."""
    transit_system = sa.table(
        "transit_system", sa.column("name", sa.String), sa.column("active", sa.Boolean)
    )
    bind = op.get_bind()
    bind.execute(
        sa.update(transit_system)
        .where(transit_system.c.name.in_(_NAMES))
        .values(active=True)
    )
