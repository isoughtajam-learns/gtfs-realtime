"""rotate 511.org API key

Revision ID: d7f2a4c6e9b1
Revises: b9d4e2f8a3c1
Create Date: 2026-09-26T00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from src.settings import get_settings

# revision identifiers, used by Alembic.
revision: str = 'd7f2a4c6e9b1'
down_revision: Union[str, Sequence[str], None] = 'b9d4e2f8a3c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The previous 511.org key was hardcoded directly in two earlier migrations
# (3b7c5d9e1f2a_add_sf_mta.py, 5e8a1c3f7b2d_add_alerts_url.py) and flagged by
# GitGuardian once this repo went public - already-merged migration files are
# left as-is (rewriting merged migration history is its own risk, and the old
# key must be treated as compromised/rotated away regardless of what those
# files literally say), but this migration re-points every URL that used it
# at the new key, read live from the environment rather than written into
# this file - see src/settings.py's api_key_511_org. Only SF-MTA is affected;
# every other 511.org-backed system gets its own onboarding migration that
# reads the key the same way from the start.
_NAME = "SF-MTA"
_AGENCY = "SF"


def upgrade() -> None:
    """Upgrade schema."""
    api_key = get_settings().api_key_511_org
    if not api_key:
        raise RuntimeError(
            "API_KEY_511_ORG is not set - this migration rotates every "
            "511.org-backed URL to the new key and refuses to run without "
            "one, rather than silently leaving the old (compromised) key in "
            "place or writing an unauthenticated URL."
        )

    transit_system = sa.table(
        'transit_system',
        sa.column('name', sa.String),
        sa.column('realtime_url', sa.String),
        sa.column('alerts_url', sa.String),
        sa.column('schedule_url', sa.String),
    )
    bind = op.get_bind()
    bind.execute(
        sa.update(transit_system)
        .where(transit_system.c.name == _NAME)
        .values(
            realtime_url=(
                f"https://api.511.org/Transit/TripUpdates"
                f"?api_key={api_key}&agency={_AGENCY}"
            ),
            alerts_url=(
                f"https://api.511.org/Transit/ServiceAlerts"
                f"?api_key={api_key}&agency={_AGENCY}"
            ),
            schedule_url=(
                f"http://api.511.org/transit/datafeeds"
                f"?api_key={api_key}&operator_id={_AGENCY}"
            ),
        )
    )


def downgrade() -> None:
    """Downgrade schema.

    Deliberately a no-op: there's no old key left to revert to (it's
    compromised/rotated away, and re-hardcoding it here to support a
    downgrade path would recreate the exact problem this migration exists
    to fix). Rolling this migration back just means SF-MTA keeps whatever
    key upgrade() last wrote - not ideal, but strictly safer than the
    alternative.
    """
    pass
