"""
DB lookup backing GET /transit_systems/{transit_system}: system-level
metadata (currently just its GTFS Schedule timezone) that changes rarely -
a plain per-request query, not cached like ScheduleCache, since this is
called far less often than the SSE hot path that justifies that cache.
"""

from typing import Optional

from sqlalchemy import select

from src.database import engine
from src.models import TransitSystem


def get_transit_system_timezone(transit_system: str) -> Optional[str]:
    with engine.begin() as connection:
        return connection.execute(
            select(TransitSystem.timezone).where(TransitSystem.name == transit_system)
        ).scalar()
