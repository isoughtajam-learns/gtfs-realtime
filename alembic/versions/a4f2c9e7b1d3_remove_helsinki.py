"""remove Helsinki

Revision ID: a4f2c9e7b1d3
Revises: 5e8a1c3f7b2d
Create Date: 2026-09-16T00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4f2c9e7b1d3'
down_revision: Union[str, Sequence[str], None] = '5e8a1c3f7b2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NAME = "Helsinki_Regional_Transport"

# Snapshot of the row this migration deletes, so downgrade() can restore the
# transit_system row itself (config only - see downgrade()'s docstring for
# why trip/stop/stop_time/route data can't come back this way). Confirmed
# live values as of 2026-09-16.
_REALTIME_URL = "https://realtime.hsl.fi/realtime/trip-updates/v2/hsl"
_SCHEDULE_URL = "http://dev.hsl.fi/gtfs/hsl.zip"
_DEFAULT_SCHEDULE_URL = "https://www.hsl.fi/en/timetables"


def upgrade() -> None:
    """Removes Helsinki_Regional_Transport entirely - Phase 3 shifted scope
    to Bay Area systems only (see .claude/plans/development.md), and this
    system was never actually served to end users. No FK is set up with
    ON DELETE CASCADE (see models.py) - route/trip/stop/stop_time rows are
    deleted explicitly, child tables first, before the transit_system row
    itself.

    No-ops if the row is already gone (e.g. this migration re-runs against
    a database where it already applied), same guard pattern as
    3b7c5d9e1f2a's SF-MTA upsert."""
    transit_system = sa.table(
        'transit_system', sa.column('id', sa.Integer), sa.column('name', sa.String)
    )
    bind = op.get_bind()
    transit_system_id = bind.execute(
        sa.select(transit_system.c.id).where(transit_system.c.name == _NAME)
    ).scalar()
    if transit_system_id is None:
        return

    for table_name in ("stop_time", "stop", "trip", "route"):
        table = sa.table(
            table_name, sa.column('transit_system_id', sa.Integer)
        )
        bind.execute(
            sa.delete(table).where(table.c.transit_system_id == transit_system_id)
        )
    bind.execute(sa.delete(transit_system).where(transit_system.c.id == transit_system_id))


def downgrade() -> None:
    """Restores the transit_system row (config only), inactive - matching
    how an existing-but-disabled system (e.g. Kiev) is represented, so a
    re-fetch can bring it back to active use deliberately rather than this
    downgrade silently reactivating it. Does NOT restore route/trip/stop/
    stop_time data - that was real GTFS Schedule ingestion (hundreds of
    thousands of rows), not something a migration should try to
    reconstruct. Run `uv run python -m src.commands.fetcher --force
    --transit-system Helsinki_Regional_Transport` after downgrading if the
    Schedule data itself needs to come back too."""
    transit_system = sa.table(
        'transit_system',
        sa.column('name', sa.String),
        sa.column('realtime_url', sa.String),
        sa.column('schedule_url', sa.String),
        sa.column('default_schedule_url', sa.String),
        sa.column('auth_required', sa.Boolean),
        sa.column('active', sa.Boolean),
        sa.column('min_poll_interval_seconds', sa.Integer),
    )
    bind = op.get_bind()
    bind.execute(
        sa.insert(transit_system).values(
            name=_NAME,
            realtime_url=_REALTIME_URL,
            schedule_url=_SCHEDULE_URL,
            default_schedule_url=_DEFAULT_SCHEDULE_URL,
            auth_required=False,
            active=False,
            min_poll_interval_seconds=0,
        )
    )
