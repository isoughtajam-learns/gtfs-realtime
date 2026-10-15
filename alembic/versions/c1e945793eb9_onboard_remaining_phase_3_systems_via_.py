"""Onboard remaining phase 3 systems via the regional feed

Revision ID: c1e945793eb9
Revises: 4cbf3d28357a
Create Date: 2026-10-15 00:29:18.376328

IRL-17: the last 7 systems from the original "phase 3" plan (see IRL-7's
findings), onboarded straight onto the regional feed from day one -
unlike SF-MTA, which was onboarded with its own direct fetch long before
agency=RG existed and only cut over later (IRL-16), these get
realtime_feed_agency_code set immediately, so RegionalFeedScheduler
serves them with zero added scheduler cost (the whole point of this
design - see docs/rate-limiting-and-scheduling.md).

realtime_url/alerts_url/schedule_url are still set to each system's own
direct 511.org URL, same as every other system - RegionalFeedScheduler
never reads them in normal operation, but RealtimeFeedCache.get()'s
demand-driven recovery path does if the scheduler ever falls behind (see
quota_group_scheduler.py's module docstring), so a regional-fed system
still needs a real, working direct URL on file, not a placeholder.

default_schedule_url is each agency's plain official homepage (confirmed
directly via web search, not guessed) rather than a specific routes/
schedules sub-page - only SF Bay Ferry's and Golden Gate Transit's
specific sub-pages could be confirmed with confidence; using the
homepage consistently for all 7 beats guessing a sub-page URL for the
rest. Can be refined per-agency later; this is just the
missing-route_url fallback (see schedule_utils.resolve_route_url), not
load-bearing for the regional-feed cutover itself.

min_poll_interval_seconds=900 matches SF-MTA's current value (see
b9d4e2f8a3c1_add_quota_group.py) - high enough that the demand-driven
recovery path essentially never fires under normal scheduler operation,
rather than competing with RegionalFeedScheduler's own carefully-paced
spend against the same shared 511.org quota.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from src.settings import get_settings


# revision identifiers, used by Alembic.
revision: str = 'c1e945793eb9'
down_revision: Union[str, Sequence[str], None] = '4cbf3d28357a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MIN_POLL_INTERVAL_SECONDS = 900

_SYSTEMS = {
    "Caltrain": {
        "agency_code": "CT",
        "default_schedule_url": "https://www.caltrain.com",
    },
    "VTA": {
        "agency_code": "SC",
        "default_schedule_url": "https://www.vta.org",
    },
    "AC_Transit": {
        "agency_code": "AC",
        "default_schedule_url": "https://www.actransit.org",
    },
    "Golden_Gate_Transit": {
        "agency_code": "GG",
        "default_schedule_url": (
            "https://www.goldengate.org/bus/riding-the-bus/find-your-route-stop"
        ),
    },
    "SamTrans": {
        "agency_code": "SM",
        "default_schedule_url": "https://www.samtrans.com",
    },
    "SF_Bay_Ferry": {
        "agency_code": "SB",
        "default_schedule_url": "https://www.sanfranciscobayferry.com/routes-and-schedules",
    },
    "County_Connection": {
        "agency_code": "CC",
        "default_schedule_url": "https://countyconnection.com",
    },
}


def upgrade() -> None:
    """Upgrade schema."""
    api_key = get_settings().api_key_511_org
    if not api_key:
        raise RuntimeError(
            "API_KEY_511_ORG is not set - this migration writes every new "
            "system's realtime_url/alerts_url/schedule_url with it, and "
            "refuses to run without one rather than writing an "
            "unauthenticated URL."
        )

    transit_system = sa.table(
        "transit_system",
        sa.column("name", sa.String),
        sa.column("realtime_url", sa.String),
        sa.column("alerts_url", sa.String),
        sa.column("schedule_url", sa.String),
        sa.column("default_schedule_url", sa.String),
        sa.column("auth_required", sa.Boolean),
        sa.column("active", sa.Boolean),
        sa.column("min_poll_interval_seconds", sa.Integer),
        sa.column("quota_group", sa.String),
        sa.column("realtime_feed_agency_code", sa.String),
    )
    bind = op.get_bind()
    for name, config in _SYSTEMS.items():
        code = config["agency_code"]
        bind.execute(
            sa.insert(transit_system).values(
                name=name,
                realtime_url=(
                    f"https://api.511.org/Transit/TripUpdates"
                    f"?api_key={api_key}&agency={code}"
                ),
                alerts_url=(
                    f"https://api.511.org/Transit/ServiceAlerts"
                    f"?api_key={api_key}&agency={code}"
                ),
                schedule_url=(
                    f"http://api.511.org/transit/datafeeds"
                    f"?api_key={api_key}&operator_id={code}"
                ),
                default_schedule_url=config["default_schedule_url"],
                auth_required=True,
                active=True,
                min_poll_interval_seconds=_MIN_POLL_INTERVAL_SECONDS,
                quota_group="511.org",
                realtime_feed_agency_code=code,
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    transit_system = sa.table("transit_system", sa.column("name", sa.String))
    bind = op.get_bind()
    bind.execute(
        sa.delete(transit_system).where(transit_system.c.name.in_(_SYSTEMS.keys()))
    )
