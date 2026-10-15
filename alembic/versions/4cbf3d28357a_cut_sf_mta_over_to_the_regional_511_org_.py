"""Cut SF-MTA over to the regional 511.org feed

Revision ID: 4cbf3d28357a
Revises: 2758dc89a61a
Create Date: 2026-10-14 22:59:13.415272

IRL-16: the agency=RG demux has been confirmed correct for both
TripUpdates and ServiceAlerts against live SF-MTA data (IRL-15's
findings) - this sets realtime_feed_agency_code so RegionalFeedScheduler
(src/services/quota_group_scheduler.py) actually serves SF-MTA from the
combined regional fetch instead of its own direct realtime_url/
alerts_url request. get_quota_group_members() excludes a system with
this set, so QuotaGroupScheduler's round-robin stops fetching SF-MTA
directly once this lands - no risk of the two schedulers racing to fetch
the same system.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4cbf3d28357a'
down_revision: Union[str, Sequence[str], None] = '2758dc89a61a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NAME = "SF-MTA"
_AGENCY_CODE = "SF"


def upgrade() -> None:
    """Upgrade schema."""
    transit_system = sa.table(
        "transit_system",
        sa.column("name", sa.String),
        sa.column("realtime_feed_agency_code", sa.String),
    )
    op.execute(
        transit_system.update()
        .where(transit_system.c.name == _NAME)
        .values(realtime_feed_agency_code=_AGENCY_CODE)
    )


def downgrade() -> None:
    """Downgrade schema."""
    transit_system = sa.table(
        "transit_system",
        sa.column("name", sa.String),
        sa.column("realtime_feed_agency_code", sa.String),
    )
    op.execute(
        transit_system.update()
        .where(transit_system.c.name == _NAME)
        .values(realtime_feed_agency_code=None)
    )
