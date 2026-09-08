"""add SF-MTA

Revision ID: 3b7c5d9e1f2a
Revises: 9d1e2f4a6c8b
Create Date: 2026-09-08T00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3b7c5d9e1f2a'
down_revision: Union[str, Sequence[str], None] = '9d1e2f4a6c8b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 511.org auth is a URL query param (api_key=...), not an HTTP header -
# that's why auth_required is set here even though nothing in the codebase
# reads it yet (schema-only scaffolding, see models.TransitSystem) and
# there's no --auth-header on these URLs.
_NAME = "SF-MTA"
_REALTIME_URL = (
    "https://api.511.org/Transit/TripUpdates"
    "?api_key=061b41da-0268-40c2-adb6-c18bd1d4389b&agency=SF"
)
_SCHEDULE_URL = (
    "http://api.511.org/transit/datafeeds"
    "?api_key=061b41da-0268-40c2-adb6-c18bd1d4389b&operator_id=SF"
)
_DEFAULT_SCHEDULE_URL = "https://www.sfmta.com/getting-around/muni/routes-stops"
_MIN_POLL_INTERVAL_SECONDS = 240


def upgrade() -> None:
    """Upgrade schema."""
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
    existing = bind.execute(
        sa.select(transit_system.c.name).where(transit_system.c.name == _NAME)
    ).first()
    values = dict(
        realtime_url=_REALTIME_URL,
        schedule_url=_SCHEDULE_URL,
        default_schedule_url=_DEFAULT_SCHEDULE_URL,
        auth_required=True,
        active=True,
        min_poll_interval_seconds=_MIN_POLL_INTERVAL_SECONDS,
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
