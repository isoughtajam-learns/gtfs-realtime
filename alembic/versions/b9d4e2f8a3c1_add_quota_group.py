"""add quota_group

Revision ID: b9d4e2f8a3c1
Revises: a4f2c9e7b1d3
Create Date: 2026-09-23T00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9d4e2f8a3c1'
down_revision: Union[str, Sequence[str], None] = 'a4f2c9e7b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# SF-MTA's min_poll_interval_seconds (240s) was tuned purely against its
# own solo 60/hour 511.org quota - now that SharedFeedQuota coordinates
# real fetch spend across every 511.org-backed system together (see
# src/services/shared_feed_quota.py), that per-system floor can drop back
# down to match the SSE loop's own natural cadence (30s): the group budget,
# not this value, is what actually protects the underlying 60/hour API-key
# limit now. Pre-migration value kept here so downgrade() can restore it -
# rolling this migration back removes the only real protection
# SharedFeedQuota was providing, so leaving min_poll_interval_seconds at 30
# post-downgrade would let SF-MTA alone burn through the real quota fast.
_SF_MTA_PRE_MIGRATION_MIN_POLL_INTERVAL_SECONDS = 240
_SF_MTA_POST_MIGRATION_MIN_POLL_INTERVAL_SECONDS = 30


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'transit_system', sa.Column('quota_group', sa.String(), nullable=True)
    )

    transit_system = sa.table(
        'transit_system',
        sa.column('name', sa.String),
        sa.column('quota_group', sa.String),
        sa.column('min_poll_interval_seconds', sa.Integer),
    )
    bind = op.get_bind()
    bind.execute(
        sa.update(transit_system)
        .where(transit_system.c.name == 'SF-MTA')
        .values(
            quota_group='511.org',
            min_poll_interval_seconds=_SF_MTA_POST_MIGRATION_MIN_POLL_INTERVAL_SECONDS,
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    transit_system = sa.table(
        'transit_system',
        sa.column('name', sa.String),
        sa.column('min_poll_interval_seconds', sa.Integer),
    )
    bind = op.get_bind()
    bind.execute(
        sa.update(transit_system)
        .where(transit_system.c.name == 'SF-MTA')
        .values(
            min_poll_interval_seconds=_SF_MTA_PRE_MIGRATION_MIN_POLL_INTERVAL_SECONDS
        )
    )
    op.drop_column('transit_system', 'quota_group')
