"""add Caltrain

Revision ID: c3e7a1f9b5d2
Revises: b9d4e2f8a3c1
Create Date: 2026-09-25T00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3e7a1f9b5d2'
down_revision: Union[str, Sequence[str], None] = 'b9d4e2f8a3c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Confirmed live (2026-09-25): TripUpdates/ServiceAlerts both return real
# entities, schedule zip diagnoses with usable stops (60/64 - passes the
# fetcher's ingestion gate, same bar as every other system on file).
# routes.txt is missing route_url on every row, hence default_schedule_url.
_NAME = "Caltrain"
_REALTIME_URL = (
    "https://api.511.org/Transit/TripUpdates"
    "?api_key=061b41da-0268-40c2-adb6-c18bd1d4389b&agency=CT"
)
_ALERTS_URL = (
    "https://api.511.org/Transit/ServiceAlerts"
    "?api_key=061b41da-0268-40c2-adb6-c18bd1d4389b&agency=CT"
)
_SCHEDULE_URL = (
    "http://api.511.org/transit/datafeeds"
    "?api_key=061b41da-0268-40c2-adb6-c18bd1d4389b&operator_id=CT"
)
_DEFAULT_SCHEDULE_URL = "https://www.caltrain.com"
# The scheduler (src/services/quota_group_scheduler.py) is what actually
# keeps this system's cache warm on a predictable cadence now - this is
# purely the demand-driven recovery threshold, matching SF-MTA's own
# post-scheduler value (see b9d4e2f8a3c1's docstring for why it's this
# high rather than tuned to any particular poll cadence).
_MIN_POLL_INTERVAL_SECONDS = 900
_QUOTA_GROUP = "511.org"


def upgrade() -> None:
    """Upgrade schema."""
    transit_system = sa.table(
        'transit_system',
        sa.column('name', sa.String),
        sa.column('realtime_url', sa.String),
        sa.column('alerts_url', sa.String),
        sa.column('schedule_url', sa.String),
        sa.column('default_schedule_url', sa.String),
        sa.column('auth_required', sa.Boolean),
        sa.column('active', sa.Boolean),
        sa.column('min_poll_interval_seconds', sa.Integer),
        sa.column('quota_group', sa.String),
    )
    bind = op.get_bind()
    existing = bind.execute(
        sa.select(transit_system.c.name).where(transit_system.c.name == _NAME)
    ).first()
    values = dict(
        realtime_url=_REALTIME_URL,
        alerts_url=_ALERTS_URL,
        schedule_url=_SCHEDULE_URL,
        default_schedule_url=_DEFAULT_SCHEDULE_URL,
        auth_required=True,
        active=True,
        min_poll_interval_seconds=_MIN_POLL_INTERVAL_SECONDS,
        quota_group=_QUOTA_GROUP,
    )
    if existing:
        bind.execute(
            sa.update(transit_system)
            .where(transit_system.c.name == _NAME)
            .values(**values)
        )
    else:
        bind.execute(sa.insert(transit_system).values(name=_NAME, **values))


def downgrade() -> None:
    """Downgrade schema."""
    transit_system = sa.table('transit_system', sa.column('name', sa.String))
    bind = op.get_bind()
    bind.execute(sa.delete(transit_system).where(transit_system.c.name == _NAME))
