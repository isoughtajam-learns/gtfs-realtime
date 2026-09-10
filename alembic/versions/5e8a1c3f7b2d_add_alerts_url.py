"""add alerts_url

Revision ID: 5e8a1c3f7b2d
Revises: 3b7c5d9e1f2a
Create Date: 2026-09-09T00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5e8a1c3f7b2d'
down_revision: Union[str, Sequence[str], None] = '3b7c5d9e1f2a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Confirmed live (real alert entities returned, not just a 200): BART's is
# a guessed sibling of its known realtime_url pattern that happened to be
# right; SF-MTA's follows 511.org's own TripUpdates/ServiceAlerts sibling
# convention. Only these two are populated - issue #16 scoped to Bay Area
# systems only (MBTA/NY_Waterway/Helsinki also have real alerts feeds,
# confirmed live, but are out of scope per that decision).
_ALERTS_URLS = {
    "BART": "https://api.bart.gov/gtfsrt/alerts.aspx",
    "SF-MTA": (
        "https://api.511.org/Transit/ServiceAlerts"
        "?api_key=061b41da-0268-40c2-adb6-c18bd1d4389b&agency=SF"
    ),
}


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'transit_system', sa.Column('alerts_url', sa.String(), nullable=True)
    )

    transit_system = sa.table(
        'transit_system',
        sa.column('name', sa.String),
        sa.column('alerts_url', sa.String),
    )
    bind = op.get_bind()
    for name, url in _ALERTS_URLS.items():
        bind.execute(
            sa.update(transit_system)
            .where(transit_system.c.name == name)
            .values(alerts_url=url)
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('transit_system', 'alerts_url')
