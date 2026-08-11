"""
In-memory cache for GTFS Schedule lookups (trip_headsign, stop_name) keyed by transit system.

The realtime feed only supplies trip_id and stop_id. Hydrating each SSE event with a DB
query would add a round-trip per entity — instead we preload the two dicts once per
transit system and refresh on a TTL, since Schedule data updates at most daily.
"""
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from sqlalchemy import select

from src.database import engine
from src.models import TransitSystem, Trip, Stop, Route


TTL = timedelta(hours=6)


class ScheduleCache:
    _trips: Dict[str, Dict[str, Optional[str]]] = {}
    _stops: Dict[str, Dict[str, str]] = {}
    _headsigns_by_route_dir: Dict[str, Dict[Tuple[str, Optional[int]], str]] = {}
    _colors_by_trip: Dict[str, Dict[str, Tuple[str, str]]] = {}
    _colors_by_route: Dict[str, Dict[str, Tuple[str, str]]] = {}
    _colors_by_stop: Dict[str, Dict[str, Tuple[str, str]]] = {}
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
                cls._loaded_at[transit_system] = datetime.utcnow()
                return
            trip_rows = conn.execute(
                select(Trip.trip_id, Trip.name, Trip.route_id, Trip.direction_id)
                .where(Trip.transit_system_id == ts_id)
            ).all()
            stop_rows = conn.execute(
                select(Stop.stop_id, Stop.name, Stop.trip_id).where(Stop.transit_system_id == ts_id)
            ).all()
            route_rows = conn.execute(
                select(Route.route_id, Route.color, Route.text_color)
                .where(Route.transit_system_id == ts_id)
            ).all()

        colors_by_route: Dict[str, Tuple[str, str]] = {
            row.route_id: (row.color, row.text_color) for row in route_rows
        }

        trips: Dict[str, Optional[str]] = {}
        headsigns_by_route_dir: Dict[Tuple[str, Optional[int]], str] = {}
        colors_by_trip: Dict[str, Tuple[str, str]] = {}
        route_id_by_trip: Dict[str, str] = {}
        for row in trip_rows:
            trips[row.trip_id] = row.name
            if row.name and row.route_id is not None:
                headsigns_by_route_dir.setdefault((row.route_id, row.direction_id), row.name)
                headsigns_by_route_dir.setdefault((row.route_id, None), row.name)
            if row.route_id:
                route_id_by_trip[row.trip_id] = row.route_id
                route_colors = colors_by_route.get(row.route_id)
                if route_colors:
                    colors_by_trip[row.trip_id] = route_colors

        colors_by_stop: Dict[str, Tuple[str, str]] = {}
        for row in stop_rows:
            route_id = route_id_by_trip.get(row.trip_id) if row.trip_id else None
            route_colors = colors_by_route.get(route_id) if route_id else None
            if route_colors:
                colors_by_stop[row.stop_id] = route_colors

        cls._trips[transit_system] = trips
        cls._headsigns_by_route_dir[transit_system] = headsigns_by_route_dir
        cls._colors_by_trip[transit_system] = colors_by_trip
        cls._colors_by_route[transit_system] = colors_by_route
        cls._colors_by_stop[transit_system] = colors_by_stop
        cls._stops[transit_system] = {row.stop_id: row.name for row in stop_rows}
        cls._loaded_at[transit_system] = datetime.utcnow()
