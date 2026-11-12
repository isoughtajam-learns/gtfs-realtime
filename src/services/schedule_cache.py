"""
In-memory cache for GTFS Schedule lookups (trip_headsign, stop_name) keyed by transit system.

The realtime feed only supplies trip_id and stop_id. Hydrating each SSE event with a DB
query would add a round-trip per entity — instead we preload these lookups once per
transit system and refresh on a TTL, since Schedule data updates at most daily.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select

from src.database import engine
from src.models import (
    Calendar,
    CalendarDate,
    IntraSystemTransfer,
    TransitSystem,
    Trip,
    Stop,
    StopTime,
    Route,
)
from src.services.schedule_utils import CalendarRow, CalendarDateRow
from src.services.transfers import query_intra_transfers


TTL = timedelta(hours=6)


class ScheduleCache:
    _trips: Dict[str, Dict[str, Optional[str]]] = {}
    _stops: Dict[str, Dict[str, str]] = {}
    _headsigns_by_route_dir: Dict[str, Dict[Tuple[str, Optional[int]], str]] = {}
    _colors_by_trip: Dict[str, Dict[str, Tuple[str, str]]] = {}
    _colors_by_route: Dict[str, Dict[str, Tuple[str, str]]] = {}
    _colors_by_stop: Dict[str, Dict[str, Tuple[str, str]]] = {}
    _short_names_by_trip: Dict[str, Dict[str, str]] = {}
    _short_names_by_route: Dict[str, Dict[str, str]] = {}
    _long_names_by_trip: Dict[str, Dict[str, str]] = {}
    _long_names_by_route: Dict[str, Dict[str, str]] = {}
    _route_id_by_trip: Dict[str, Dict[str, str]] = {}
    # Phase 3 (see .claude/plans/development.md) - intra-system transfers
    # reachable from each stop, keyed by stop_id. See
    # src/services/transfers.py's query_intra_transfers for the query itself
    # and why it's shared with trip_detail.py's narrower, per-request version.
    _intra_transfers_by_stop: Dict[str, Dict[str, List[IntraSystemTransfer]]] = {}
    # Operating-hours awareness (see src/services/schedule_utils.py's
    # is_outside_operating_hours) - plain, small row lists (at most a few
    # hundred rows even for a large system), not the full StopTime table.
    _calendar_rows: Dict[str, List[CalendarRow]] = {}
    _calendar_date_rows: Dict[str, List[CalendarDateRow]] = {}
    # service_id -> (min_departure_seconds, max_arrival_seconds), from one
    # aggregate SQL query - never raw stop_time rows (Helsinki's table is
    # 7.8M rows/~700MB; a GROUP BY aggregate returns at most one row per
    # distinct service_id regardless of system size).
    _operating_windows_by_service_id: Dict[str, Dict[str, Tuple[int, int]]] = {}
    _loaded_at: Dict[str, datetime] = {}
    _locks: Dict[str, asyncio.Lock] = {}

    @classmethod
    async def get(
        cls, transit_system: str
    ) -> Tuple[
        Dict[str, Optional[str]],
        Dict[str, str],
        Dict[Tuple[str, Optional[int]], str],
        Dict[str, Tuple[str, str]],
        Dict[str, Tuple[str, str]],
        Dict[str, Tuple[str, str]],
        Dict[str, str],
        Dict[str, str],
        Dict[str, str],
        Dict[str, str],
        Dict[str, str],
        Dict[str, List[IntraSystemTransfer]],
        List[CalendarRow],
        List[CalendarDateRow],
        Dict[str, Tuple[int, int]],
    ]:
        if not cls._is_fresh(transit_system):
            lock = cls._locks.setdefault(transit_system, asyncio.Lock())
            async with lock:
                if not cls._is_fresh(transit_system):
                    await asyncio.to_thread(cls._load, transit_system)
        return (
            cls._trips.get(transit_system, {}),
            cls._stops.get(transit_system, {}),
            cls._headsigns_by_route_dir.get(transit_system, {}),
            cls._colors_by_trip.get(transit_system, {}),
            cls._colors_by_route.get(transit_system, {}),
            cls._colors_by_stop.get(transit_system, {}),
            cls._short_names_by_trip.get(transit_system, {}),
            cls._short_names_by_route.get(transit_system, {}),
            cls._long_names_by_trip.get(transit_system, {}),
            cls._long_names_by_route.get(transit_system, {}),
            cls._route_id_by_trip.get(transit_system, {}),
            cls._intra_transfers_by_stop.get(transit_system, {}),
            cls._calendar_rows.get(transit_system, []),
            cls._calendar_date_rows.get(transit_system, []),
            cls._operating_windows_by_service_id.get(transit_system, {}),
        )

    @classmethod
    def _is_fresh(cls, transit_system: str) -> bool:
        loaded = cls._loaded_at.get(transit_system)
        return loaded is not None and datetime.utcnow() - loaded < TTL

    @classmethod
    def _load(cls, transit_system: str) -> None:
        with engine.begin() as conn:
            ts_id = conn.execute(
                select(TransitSystem.id).where(TransitSystem.name == transit_system)
            ).scalar()
            if ts_id is None:
                cls._trips[transit_system] = {}
                cls._stops[transit_system] = {}
                cls._headsigns_by_route_dir[transit_system] = {}
                cls._colors_by_trip[transit_system] = {}
                cls._colors_by_route[transit_system] = {}
                cls._colors_by_stop[transit_system] = {}
                cls._short_names_by_trip[transit_system] = {}
                cls._short_names_by_route[transit_system] = {}
                cls._long_names_by_trip[transit_system] = {}
                cls._long_names_by_route[transit_system] = {}
                cls._route_id_by_trip[transit_system] = {}
                cls._intra_transfers_by_stop[transit_system] = {}
                cls._calendar_rows[transit_system] = []
                cls._calendar_date_rows[transit_system] = []
                cls._operating_windows_by_service_id[transit_system] = {}
                cls._loaded_at[transit_system] = datetime.utcnow()
                return
            trip_rows = conn.execute(
                select(Trip.trip_id, Trip.name, Trip.route_id, Trip.direction_id).where(
                    Trip.transit_system_id == ts_id
                )
            ).all()
            stop_rows = conn.execute(
                select(Stop.stop_id, Stop.name, Stop.trip_id).where(
                    Stop.transit_system_id == ts_id
                )
            ).all()
            route_rows = conn.execute(
                select(
                    Route.route_id,
                    Route.color,
                    Route.text_color,
                    Route.short_name,
                    Route.long_name,
                ).where(Route.transit_system_id == ts_id)
            ).all()
            intra_transfers_by_stop = query_intra_transfers(conn, ts_id)
            calendar_rows_raw = conn.execute(
                select(
                    Calendar.service_id,
                    Calendar.monday,
                    Calendar.tuesday,
                    Calendar.wednesday,
                    Calendar.thursday,
                    Calendar.friday,
                    Calendar.saturday,
                    Calendar.sunday,
                    Calendar.start_date,
                    Calendar.end_date,
                ).where(Calendar.transit_system_id == ts_id)
            ).all()
            calendar_date_rows_raw = conn.execute(
                select(
                    CalendarDate.service_id,
                    CalendarDate.date,
                    CalendarDate.exception_type,
                ).where(CalendarDate.transit_system_id == ts_id)
            ).all()
            # Aggregate only - never raw stop_time rows (see this module's
            # docstring reference and StopTime's own docstring on
            # Helsinki's 7.8M-row table). Returns at most one row per
            # distinct service_id.
            operating_window_rows = conn.execute(
                select(
                    Trip.service_id,
                    func.min(StopTime.departure_time_seconds),
                    func.max(StopTime.arrival_time_seconds),
                )
                .select_from(StopTime)
                .join(
                    Trip,
                    (Trip.transit_system_id == StopTime.transit_system_id)
                    & (Trip.trip_id == StopTime.trip_id),
                )
                .where(
                    StopTime.transit_system_id == ts_id,
                    Trip.service_id.is_not(None),
                )
                .group_by(Trip.service_id)
            ).all()

        # Filtered on truthiness like short_names_by_route/long_names_by_route
        # below - a route can now have color=text_color=None (see
        # missing_route_fields(), no longer required per the GTFS spec), and
        # an unfiltered (None, None) entry here would be truthy, short-
        # circuiting main.py's `colors_by_trip.get(...) or
        # colors_by_route.get(...) or colors_by_stop...` fallback chain
        # before it ever reaches the colors_by_stop fallback.
        colors_by_route: Dict[str, Tuple[str, str]] = {
            row.route_id: (row.color, row.text_color)
            for row in route_rows
            if row.color and row.text_color
        }
        # route_short_name/route_long_name are real-world signage/colloquial
        # route naming (e.g. SF Muni's "L" short_name + "TARAVAL" long_name,
        # together read as "L Taraval") - confirmed live: SF-MTA's own
        # service alerts refer to routes by short_name ("48 RRT STOP
        # CLOSED"), but the live trip_update event previously carried
        # nothing but trip_headsign, leaving no visible link between an
        # alert and the trips it's about. Keyed same as colors_by_route.
        short_names_by_route: Dict[str, str] = {
            row.route_id: row.short_name for row in route_rows if row.short_name
        }
        long_names_by_route: Dict[str, str] = {
            row.route_id: row.long_name for row in route_rows if row.long_name
        }

        trips: Dict[str, Optional[str]] = {}
        headsigns_by_route_dir: Dict[Tuple[str, Optional[int]], str] = {}
        colors_by_trip: Dict[str, Tuple[str, str]] = {}
        short_names_by_trip: Dict[str, str] = {}
        long_names_by_trip: Dict[str, str] = {}
        route_id_by_trip: Dict[str, str] = {}
        for row in trip_rows:
            trips[row.trip_id] = row.name
            if row.name and row.route_id is not None:
                headsigns_by_route_dir.setdefault(
                    (row.route_id, row.direction_id), row.name
                )
                headsigns_by_route_dir.setdefault((row.route_id, None), row.name)
            if row.route_id:
                route_id_by_trip[row.trip_id] = row.route_id
                route_colors = colors_by_route.get(row.route_id)
                if route_colors:
                    colors_by_trip[row.trip_id] = route_colors
                route_short_name = short_names_by_route.get(row.route_id)
                if route_short_name:
                    short_names_by_trip[row.trip_id] = route_short_name
                route_long_name = long_names_by_route.get(row.route_id)
                if route_long_name:
                    long_names_by_trip[row.trip_id] = route_long_name

        colors_by_stop: Dict[str, Tuple[str, str]] = {}
        for stop_row in stop_rows:
            route_id = (
                route_id_by_trip.get(stop_row.trip_id) if stop_row.trip_id else None
            )
            route_colors = colors_by_route.get(route_id) if route_id else None
            if route_colors:
                colors_by_stop[stop_row.stop_id] = route_colors

        cls._trips[transit_system] = trips
        cls._headsigns_by_route_dir[transit_system] = headsigns_by_route_dir
        cls._colors_by_trip[transit_system] = colors_by_trip
        cls._colors_by_route[transit_system] = colors_by_route
        cls._colors_by_stop[transit_system] = colors_by_stop
        cls._short_names_by_trip[transit_system] = short_names_by_trip
        cls._short_names_by_route[transit_system] = short_names_by_route
        cls._long_names_by_trip[transit_system] = long_names_by_trip
        cls._long_names_by_route[transit_system] = long_names_by_route
        cls._route_id_by_trip[transit_system] = route_id_by_trip
        cls._intra_transfers_by_stop[transit_system] = intra_transfers_by_stop
        cls._stops[transit_system] = {row.stop_id: row.name for row in stop_rows}
        cls._calendar_rows[transit_system] = [
            CalendarRow(
                service_id=row.service_id,
                weekdays=(
                    row.monday,
                    row.tuesday,
                    row.wednesday,
                    row.thursday,
                    row.friday,
                    row.saturday,
                    row.sunday,
                ),
                start_date=row.start_date,
                end_date=row.end_date,
            )
            for row in calendar_rows_raw
        ]
        cls._calendar_date_rows[transit_system] = [
            CalendarDateRow(
                service_id=row.service_id,
                date=row.date,
                exception_type=row.exception_type,
            )
            for row in calendar_date_rows_raw
        ]
        cls._operating_windows_by_service_id[transit_system] = {
            row[0]: (row[1], row[2])
            for row in operating_window_rows
            if row[1] is not None and row[2] is not None
        }
        cls._loaded_at[transit_system] = datetime.utcnow()
