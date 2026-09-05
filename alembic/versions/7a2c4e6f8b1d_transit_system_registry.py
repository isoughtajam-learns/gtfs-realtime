"""transit system registry

Revision ID: 7a2c4e6f8b1d
Revises: 5f3a8c1d9e4b
Create Date: 2026-09-05 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a2c4e6f8b1d'
down_revision: Union[str, Sequence[str], None] = '5f3a8c1d9e4b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Snapshot of src/constants.py's GTFS_URLS/GTFS_METADATA/
# DEFAULT_SCHEDULE_URL_BY_SYSTEM at the moment those dicts were deleted -
# this is the one-time seed moving their contents into TransitSystem rows.
# Adding a system from here on means inserting (or activating) a row
# directly, not editing this list.
_SEED_SYSTEMS = [
    {
        "name": "BART",
        "realtime_url": "http://api.bart.gov/gtfsrt/tripupdate.aspx",
        "schedule_url": "http://www.bart.gov/dev/schedules/google_transit.zip",
        "default_schedule_url": "https://www.bart.gov/schedules",
    },
    {
        "name": "Helsinki_Regional_Transport",
        "realtime_url": "https://realtime.hsl.fi/realtime/trip-updates/v2/hsl",
        "schedule_url": "http://dev.hsl.fi/gtfs/hsl.zip",
        "default_schedule_url": "https://www.hsl.fi/en/timetables",
    },
    {
        "name": "MBTA",
        "realtime_url": "https://cdn.mbta.com/realtime/TripUpdates.pb",
        "schedule_url": "https://cdn.mbta.com/MBTA_GTFS.zip",
        "default_schedule_url": "https://www.mbta.com/schedules/",
    },
    {
        "name": "NY_Waterway",
        "realtime_url": "https://nywaterway.connexionz.net/rtt/public/utility/gtfsrealtime.aspx/tripupdate",
        "schedule_url": "https://nywaterway.connexionz.net/rtt/public/resource/gtfs.zip",
        "default_schedule_url": None,
    },
]


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'transit_system', sa.Column('default_schedule_url', sa.String(), nullable=True)
    )
    op.add_column(
        'transit_system',
        sa.Column(
            'auth_required', sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        'transit_system',
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    transit_system = sa.table(
        'transit_system',
        sa.column('name', sa.String),
        sa.column('realtime_url', sa.String),
        sa.column('schedule_url', sa.String),
        sa.column('default_schedule_url', sa.String),
        sa.column('active', sa.Boolean),
    )
    bind = op.get_bind()
    for row in _SEED_SYSTEMS:
        existing = bind.execute(
            sa.select(transit_system.c.name).where(
                transit_system.c.name == row["name"]
            )
        ).first()
        # A row may already exist (created by a previous real fetch, back
        # when upsert_transit_system wrote realtime_url/schedule_url from
        # the constants.py dicts on every fetch) - update it in place rather
        # than inserting a duplicate, since `name` is unique. Either way,
        # `active` is explicitly set true only for systems in this seed list
        # - any other pre-existing row (e.g. Kiev, disabled by removing it
        # from constants.py rather than touching the DB) keeps the column's
        # default of false, matching its already-disabled real-world state.
        if existing:
            bind.execute(
                sa.update(transit_system)
                .where(transit_system.c.name == row["name"])
                .values(
                    realtime_url=row["realtime_url"],
                    schedule_url=row["schedule_url"],
                    default_schedule_url=row["default_schedule_url"],
                    active=True,
                )
            )
        else:
            bind.execute(
                sa.insert(transit_system).values(
                    name=row["name"],
                    realtime_url=row["realtime_url"],
                    schedule_url=row["schedule_url"],
                    default_schedule_url=row["default_schedule_url"],
                    active=True,
                )
            )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('transit_system', 'active')
    op.drop_column('transit_system', 'auth_required')
    op.drop_column('transit_system', 'default_schedule_url')
