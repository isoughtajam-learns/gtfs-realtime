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
# own solo 60/hour 511.org quota. Now that a QuotaGroupScheduler (see
# src/services/quota_group_scheduler.py) proactively, evenly re-fetches
# every 511.org-backed system on its own predictable cadence - roughly
# every ~11 minutes per system for today's group size, well under this
# value - min_poll_interval_seconds stops being "the promised refresh
# rate" and becomes purely a demand-driven recovery threshold: an inbound
# HTTP request only attempts its own real fetch if the scheduler has
# somehow fallen behind on this system for longer than this. Set high
# (900s) so it essentially never fires under normal scheduler operation
# and doesn't compete with the scheduler's own carefully-paced spend
# against the same shared budget. Pre-migration value kept here so
# downgrade() can restore it - rolling this migration back (and, with it,
# the scheduler that depends on quota_group) removes the only real
# protection left, so leaving min_poll_interval_seconds at 900
# post-downgrade would badly starve SF-MTA of any real refresh at all.
_SF_MTA_PRE_MIGRATION_MIN_POLL_INTERVAL_SECONDS = 240
_SF_MTA_POST_MIGRATION_MIN_POLL_INTERVAL_SECONDS = 900


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
