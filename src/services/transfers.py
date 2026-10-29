"""
Intra-system transfer lookups backing Phase 3 (see .claude/plans/
development.md) - explicit same-system transfers from a system's own
transfers.txt (Transfer table). Inter-system lookups live in
src/services/transfer_cache.py instead (cross-system data needs a
differently-shaped, global cache - see that module's docstring).
"""

from collections import defaultdict
from typing import Dict, List, Optional

from sqlalchemy import Connection, select

from src.models import IntraSystemTransfer, Route, Transfer


def query_intra_transfers(
    connection: Connection,
    transit_system_id: int,
    stop_ids: Optional[List[str]] = None,
) -> Dict[str, List[IntraSystemTransfer]]:
    """Every to_route_id reachable via a transfer FROM each stop_id in this
    system (Transfer.from_stop_id is the rider's current stop, to_route_id
    the route they can transfer to - see Transfer's own docstring), joined
    against Route for display. IntraSystemTransfer is a small Route summary,
    not a bare id, so the frontend can render a correctly-colored pill
    without a second lookup.

    stop_ids=None fetches every stop in the system (ScheduleCache's full
    per-system preload - see schedule_cache.py); a list scopes to just those
    stops (trip_detail's narrow, per-request batch - see
    src/services/trip_detail.py). Rows with no to_route_id (allowed by the
    GTFS spec - a transfer can apply generally at a stop rather than to one
    specific route) carry nothing to label a pill with, so they're skipped
    rather than surfaced as an empty entry.
    """
    query = (
        select(
            Transfer.from_stop_id,
            Transfer.to_route_id,
            Route.short_name,
            Route.long_name,
            Route.color,
            Route.text_color,
        )
        .select_from(Transfer)
        .join(
            Route,
            (Route.transit_system_id == Transfer.transit_system_id)
            & (Route.route_id == Transfer.to_route_id),
        )
        .where(
            Transfer.transit_system_id == transit_system_id,
            Transfer.to_route_id.is_not(None),
        )
    )
    if stop_ids is not None:
        query = query.where(Transfer.from_stop_id.in_(stop_ids))

    transfers_by_stop: Dict[str, List[IntraSystemTransfer]] = defaultdict(list)
    for row in connection.execute(query):
        transfers_by_stop[row.from_stop_id].append(
            IntraSystemTransfer(
                route_id=row.to_route_id,
                route_short_name=row.short_name,
                route_long_name=row.long_name,
                color=row.color,
                text_color=row.text_color,
            )
        )
    return dict(transfers_by_stop)
