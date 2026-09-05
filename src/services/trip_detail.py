"""
Schedule data lookups backing GET /trip_detail/{transit_system}/{trip_id}:
joins a live trip's route_id and stop_ids against what
src.commands.fetcher already ingested from the GTFS Schedule download.

Unlike ScheduleCache (preloaded, cached, sized for the high-frequency SSE
hot path across every trip in a system), these are single-trip, on-demand
lookups - plain per-request queries, not cached.
"""

from typing import Any, List, Optional, Tuple

from sqlalchemy import select

from src.database import engine
from src.models import Route, Stop, StopTime, TransitSystem, Trip


def get_trip_schedule_context(
    transit_system: str, trip_id: str, stop_ids: list[str]
) -> dict[str, Any]:
    """route_id deliberately comes from our own stored Trip row, not a
    parameter the caller passes in from the live feed: some sources (e.g.
    BART) never populate TripDescriptor.route_id on the realtime side at
    all, even though the same trip's route_id is right there in the static
    Schedule data we already ingested - trusting the live feed for it would
    silently drop the Route lookup for those sources.

    Selects specific columns (not whole ORM entities) into plain dicts
    before the connection closes, so nothing here can raise a
    detached-instance error afterward."""
    with engine.begin() as connection:
        transit_system_id = connection.execute(
            select(TransitSystem.id).where(TransitSystem.name == transit_system)
        ).scalar()
        if transit_system_id is None:
            return {"trip": None, "route": None, "stops": {}}

        trip_row = connection.execute(
            select(
                Trip.route_id,
                Trip.name,
                Trip.direction_id,
                Trip.trip_short_name,
                Trip.wheelchair_accessible,
                Trip.bikes_allowed,
            ).where(
                Trip.transit_system_id == transit_system_id, Trip.trip_id == trip_id
            )
        ).first()
        trip: Optional[dict[str, Any]] = (
            {
                "route_id": trip_row.route_id,
                "trip_headsign": trip_row.name,
                "direction_id": trip_row.direction_id,
                "trip_short_name": trip_row.trip_short_name,
                "wheelchair_accessible": trip_row.wheelchair_accessible,
                "bikes_allowed": trip_row.bikes_allowed,
            }
            if trip_row
            else None
        )

        route: Optional[dict[str, Any]] = None
        route_id = trip_row.route_id if trip_row else None
        if route_id:
            route_row = connection.execute(
                select(
                    Route.short_name,
                    Route.long_name,
                    Route.url,
                    Route.color,
                    Route.text_color,
                    Route.route_type,
                ).where(
                    Route.transit_system_id == transit_system_id,
                    Route.route_id == route_id,
                )
            ).first()
            route = (
                {
                    "route_short_name": route_row.short_name,
                    "route_long_name": route_row.long_name,
                    "route_url": route_row.url,
                    "route_color": route_row.color,
                    "route_text_color": route_row.text_color,
                    "route_type": route_row.route_type,
                }
                if route_row
                else None
            )

        stops: dict[str, dict[str, Any]] = {}
        if stop_ids:
            stop_rows = connection.execute(
                select(
                    Stop.stop_id,
                    Stop.name,
                    Stop.lat,
                    Stop.lon,
                    Stop.platform_code,
                    Stop.platform_name,
                    Stop.wheelchair_boarding,
                ).where(
                    Stop.transit_system_id == transit_system_id,
                    Stop.stop_id.in_(stop_ids),
                )
            ).all()
            stops = {
                row.stop_id: {
                    "stop_name": row.name,
                    "stop_lat": row.lat,
                    "stop_lon": row.lon,
                    "platform_code": row.platform_code,
                    "platform_name": row.platform_name,
                    "wheelchair_boarding": row.wheelchair_boarding,
                }
                for row in stop_rows
            }

        return {"trip": trip, "route": route, "stops": stops}


def _tail_stops_after_anchor(
    rows: List[Tuple[int, str]], live_stop_ids: List[str]
) -> List[Tuple[int, str]]:
    """Pure anchor-point computation, split out from get_scheduled_tail_stops
    so the turnback/dedup logic below stays directly unit-testable without
    a database (see src/tests/services/test_trip_detail.py) - mirrors
    schedule_utils.py's pattern of keeping derivation logic separate from
    I/O. `rows` must already be one trip's (stop_sequence, stop_id) pairs
    in sequence order (get_scheduled_tail_stops' SQL query guarantees this
    via its WHERE/ORDER BY).

    live_stop_ids' LAST entry isn't always unique in the static schedule:
    BART terminal-turnback stations (confirmed live: SFO on the Yellow
    line, which dwells and reverses toward Millbrae) legitimately list the
    same stop_id twice in a row in stop_times.txt - once on arrival, once
    on departure after the hold. Anchoring on the *first* static match of
    that stop_id would re-surface the second real occurrence as a bogus
    "backfilled" duplicate of a stop already shown live. Anchoring on the
    *last* static match would wrongly skip real intermediate stops for a
    trip that legitimately revisits a stop_id much later (e.g. a loop
    shuttle). Counting how many times the last-covered stop_id has
    appeared across the whole live feed so far, and anchoring on that same
    Nth static occurrence, gets both right: it only treats an occurrence
    as "already covered" once the live feed has actually reported it that
    many times.

    Returns (stop_sequence, stop_id) pairs strictly after that anchor, in
    order. Empty if `rows` is empty (trip not in Schedule at all), or if
    the live feed's last stop_id doesn't appear (at least that many times)
    in `rows` (e.g. a realtime/static ID mismatch, like MBTA's - safer to
    show nothing extra than guess where it fits)."""
    if not live_stop_ids:
        return []

    last_covered_stop_id = live_stop_ids[-1]
    occurrence_count = live_stop_ids.count(last_covered_stop_id)
    matching_sequences = [
        sequence for sequence, stop_id in rows if stop_id == last_covered_stop_id
    ]
    if len(matching_sequences) < occurrence_count:
        return []
    last_covered_sequence = matching_sequences[occurrence_count - 1]
    return [
        (sequence, stop_id)
        for sequence, stop_id in rows
        if sequence > last_covered_sequence
    ]


def get_scheduled_tail_stops(
    transit_system: str, trip_id: str, live_stop_ids: List[str]
) -> List[Tuple[int, str]]:
    """Some realtime feeds (confirmed live: BART) don't publish a
    stop_time_update for every remaining stop on a trip - the GTFS-RT spec
    explicitly allows this (its NO_DATA schedule_relationship exists for
    exactly this case: "time predictions only for part of a trip"). This
    means /trip_detail's stops[] can legitimately fall short of where the
    trip's route_long_name says the line actually ends.

    Queries the StopTime table (populated by
    src.commands.fetcher.upsert_stop_times) for this trip's static stops
    after the live feed's last-covered stop, so the response can still
    show the full scheduled route with those extra stops marked as "no
    live prediction" rather than silently ending early. Previously this
    scanned stop_times.txt directly per request - fine for most sources,
    but Helsinki's is 7.8M rows/~700MB, which made every lookup take a
    very long time; StopTime's (transit_system_id, trip_id, stop_sequence)
    index turns this into a single indexed query instead."""
    if not live_stop_ids:
        return []

    with engine.begin() as connection:
        transit_system_id = connection.execute(
            select(TransitSystem.id).where(TransitSystem.name == transit_system)
        ).scalar()
        if transit_system_id is None:
            return []

        rows = [
            (row.stop_sequence, row.stop_id)
            for row in connection.execute(
                select(StopTime.stop_sequence, StopTime.stop_id)
                .where(
                    StopTime.transit_system_id == transit_system_id,
                    StopTime.trip_id == trip_id,
                )
                .order_by(StopTime.stop_sequence)
            )
        ]

    return _tail_stops_after_anchor(rows, live_stop_ids)
