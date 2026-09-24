"""
Transit system registry: what used to be src/constants.py's GTFS_URLS/
GTFS_METADATA/DEFAULT_SCHEDULE_URL_BY_SYSTEM dicts now lives on the
TransitSystem row itself - these are the DB lookups backing both
GET /transit_systems[/​{transit_system}] and every other call site that
used to read those dicts directly (main.py's transit_feed()/trip_detail(),
tasks.py's periodic fetches, fetcher.py's CLI). Plain per-request queries,
not cached like ScheduleCache, since none of these are on the SSE hot path.
"""

from typing import Any, Optional

from sqlalchemy import distinct, select

from src.database import engine
from src.models import TransitSystem


def get_transit_system_config(transit_system: str) -> Optional[dict[str, Any]]:
    """Single-system config lookup, gated on active - an inactive system
    (has a row, e.g. Kiev, but isn't enabled - see models.TransitSystem)
    returns None here, same as one with no row at all, so callers can
    treat both as "unknown system" with one check."""
    with engine.begin() as connection:
        row = connection.execute(
            select(
                TransitSystem.realtime_url,
                TransitSystem.schedule_url,
                TransitSystem.timezone,
                TransitSystem.default_schedule_url,
                TransitSystem.auth_required,
                TransitSystem.min_poll_interval_seconds,
                TransitSystem.alerts_url,
                TransitSystem.quota_group,
            ).where(
                TransitSystem.name == transit_system, TransitSystem.active.is_(True)
            )
        ).first()
    if row is None:
        return None
    return {
        "realtime_url": row.realtime_url,
        "schedule_url": row.schedule_url,
        "timezone": row.timezone,
        "default_schedule_url": row.default_schedule_url,
        "auth_required": row.auth_required,
        "min_poll_interval_seconds": row.min_poll_interval_seconds,
        "alerts_url": row.alerts_url,
        "quota_group": row.quota_group,
    }


def get_active_transit_systems() -> list[dict[str, Any]]:
    """Replaces GTFS_URLS/GTFS_METADATA as the registry of which systems to
    serve/fetch - see models.TransitSystem.active for why a system can have
    real URLs on file without being in this list."""
    with engine.begin() as connection:
        rows = connection.execute(
            select(
                TransitSystem.name,
                TransitSystem.realtime_url,
                TransitSystem.schedule_url,
                TransitSystem.auth_required,
            ).where(TransitSystem.active.is_(True))
        ).all()
    return [
        {
            "name": row.name,
            "realtime_url": row.realtime_url,
            "schedule_url": row.schedule_url,
            "auth_required": row.auth_required,
        }
        for row in rows
    ]


def get_active_quota_groups() -> list[str]:
    """Every distinct TransitSystem.quota_group value currently in use by
    an active system - what src/services/quota_group_scheduler.py's
    lifespan() startup hook iterates to know which shared-budget
    schedulers to actually start. Queried once at startup (see that
    module) rather than kept live-updated - onboarding a new quota_group
    member already means a migration + deploy, which restarts the process
    anyway."""
    with engine.begin() as connection:
        rows = connection.execute(
            select(distinct(TransitSystem.quota_group)).where(
                TransitSystem.active.is_(True), TransitSystem.quota_group.is_not(None)
            )
        ).all()
    return [row[0] for row in rows]


def get_quota_group_members(quota_group: str) -> list[dict[str, Any]]:
    """Every active system sharing `quota_group`, ordered by name for a
    deterministic round-robin - see
    src/services/quota_group_scheduler.py's QuotaGroupScheduler."""
    with engine.begin() as connection:
        rows = connection.execute(
            select(
                TransitSystem.name, TransitSystem.realtime_url, TransitSystem.alerts_url
            )
            .where(
                TransitSystem.active.is_(True),
                TransitSystem.quota_group == quota_group,
            )
            .order_by(TransitSystem.name)
        ).all()
    return [
        {
            "name": row.name,
            "realtime_url": row.realtime_url,
            "alerts_url": row.alerts_url,
        }
        for row in rows
    ]
