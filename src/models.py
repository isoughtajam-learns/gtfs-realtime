from datetime import datetime
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import ForeignKey, ForeignKeyConstraint, UniqueConstraint, false
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Status(StrEnum):
    IN_TRANSIT = "In Transit 💨"
    AT_STOP = "Stopped 🛑"


# BaseModels for transient, runtime data
class SimplePosition(BaseModel):
    stop_id: str
    previous: int
    next: int
    status: Status
    stop_name: Optional[str] = None


class TripPosition(SimplePosition):
    trip_id: str
    trip_headsign: Optional[str] = None
    color: Optional[str] = None
    text_color: Optional[str] = None


class TripStopDetail(BaseModel):
    """One stop_time_update from the live feed, joined against our stored
    Schedule data for that stop. Unlike TripPosition (one "current" stop),
    /trip_detail returns every remaining stop on the trip."""

    stop_sequence: Optional[int] = None
    stop_id: str
    stop_name: Optional[str] = None
    stop_lat: Optional[float] = None
    stop_lon: Optional[float] = None
    platform_code: Optional[str] = None
    platform_name: Optional[str] = None
    wheelchair_boarding: Optional[int] = None
    arrival_time: Optional[int] = None
    arrival_delay: Optional[int] = None
    departure_time: Optional[int] = None
    departure_delay: Optional[int] = None
    schedule_relationship: str


class TripDetail(BaseModel):
    """Response for GET /trip_detail/{transit_system}/{trip_id} - everything
    the realtime feed says about this one trip right now, plus whatever
    Schedule data we have on its route/trip/stops."""

    trip_id: str
    route_id: Optional[str] = None
    direction_id: Optional[int] = None
    trip_headsign: Optional[str] = None
    trip_short_name: Optional[str] = None
    wheelchair_accessible: Optional[int] = None
    bikes_allowed: Optional[int] = None
    start_time: Optional[str] = None
    start_date: Optional[str] = None
    schedule_relationship: Optional[str] = None
    delay: Optional[int] = None
    timestamp: Optional[int] = None
    vehicle_id: Optional[str] = None
    vehicle_label: Optional[str] = None
    route_short_name: Optional[str] = None
    route_long_name: Optional[str] = None
    route_url: Optional[str] = None
    route_color: Optional[str] = None
    route_text_color: Optional[str] = None
    route_type: Optional[int] = None
    stops: list[TripStopDetail] = []


class TransitSystemDetail(BaseModel):
    """Response for GET /transit_systems/{transit_system} - system-level
    metadata that changes rarely (e.g. its GTFS Schedule timezone).
    Deliberately its own endpoint rather than a field on every SSE event:
    the frontend can fetch it once and cache it client-side instead of
    receiving the same static value on every single streamed trip_update."""

    name: str
    timezone: Optional[str] = None
    # Moved here from constants.py's DEFAULT_SCHEDULE_URL_BY_SYSTEM - see
    # TransitSystem.default_schedule_url.
    default_schedule_url: Optional[str] = None
    # Whether fetching this system's realtime_url needs an API secret
    # attached. Schema/API-surface only for now - no current system needs
    # auth, so there's nothing to look up yet; see
    # TransitSystem.auth_required.
    auth_required: bool = False


class ORMBase(DeclarativeBase):
    pass


class TransitSystem(ORMBase):
    __tablename__ = "transit_system"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(unique=True)
    realtime_url: Mapped[str]
    schedule_url: Mapped[Optional[str]]
    # Set by fetcher.py on every successful fetch attempt. Durable (DB-backed)
    # daily-fetch gate - the fetcher's local file-based checks live in the
    # celery-worker container's ephemeral filesystem and get wiped on every
    # restart/redeploy, so they can't be trusted to prevent repeat fetches
    # across container churn. This column can.
    last_fetched_at: Mapped[Optional[datetime]]
    # From agency.txt's agency_timezone (required by the GTFS spec, though
    # not every source complies) - an IANA identifier like
    # "America/Los_Angeles". Optional here for the same reason every other
    # Schedule-derived field is: some source failing to comply with the
    # spec shouldn't block ingestion of everything else.
    timezone: Mapped[Optional[str]]
    # Fallback Route.url when a route's own routes.txt row omits one (see
    # schedule_utils.resolve_route_url) - moved here from constants.py's
    # DEFAULT_SCHEDULE_URL_BY_SYSTEM so a system's full config lives in one
    # place (this row) instead of split across a dict literal and this table.
    default_schedule_url: Mapped[Optional[str]]
    # Whether _fetch_feed needs to attach a per-system API secret when
    # polling realtime_url. No current system needs this - added as
    # schema-only scaffolding; the actual secret lookup/attach mechanism
    # isn't wired up yet (see project memory).
    auth_required: Mapped[bool] = mapped_column(default=False, server_default=false())
    # Whether this system should be served/fetched at all. Distinct from
    # having URLs on file: a system can have real realtime_url/schedule_url
    # values without being active (e.g. Kiev, deliberately disabled - see
    # project memory). GTFS_URLS/GTFS_METADATA used to encode "enabled"
    # purely by a system's presence in those constants.py dicts; this
    # column replaces that now that the dicts are gone. Defaults false so
    # a pre-existing or future row nobody explicitly activates doesn't
    # silently start being served.
    active: Mapped[bool] = mapped_column(default=False, server_default=false())
    __table_args__ = (UniqueConstraint("name", name="uq_name"),)


class Route(ORMBase):
    __tablename__ = "route"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    transit_system_id: Mapped[int] = mapped_column(ForeignKey("transit_system.id"))
    route_id: Mapped[str]
    short_name: Mapped[str] = mapped_column(
        primary_key=True, unique=True, autoincrement=False
    )
    long_name: Mapped[str]
    url: Mapped[str]
    color: Mapped[str]
    text_color: Mapped[str]
    # GTFS numeric mode enum (0=tram, 1=subway, 2=rail, 3=bus, 4=ferry, ...) -
    # optional here for the same reason zone_id is: not every field we'd
    # like to show needs to gate whether a route is usable at all.
    route_type: Mapped[Optional[int]]

    __table_args__ = (
        UniqueConstraint("id", "short_name", name="uq_route_short_name"),
        UniqueConstraint("short_name", name="uq_short_name"),
    )


class Trip(ORMBase):
    __tablename__ = "trip"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trip_id: Mapped[str] = mapped_column(primary_key=True, autoincrement=False)
    transit_system_id: Mapped[int] = mapped_column(ForeignKey("transit_system.id"))
    route_id: Mapped[str]
    name: Mapped[Optional[str]]
    direction_id: Mapped[Optional[int]]
    # trips.txt's rider-facing train/run number (e.g. MBTA commuter rail's
    # "509") - distinct from trip_id, which is an internal identifier.
    trip_short_name: Mapped[Optional[str]]
    # GTFS 0/1/2 enums (no info / accessible / not accessible) - optional,
    # not every source publishes them.
    wheelchair_accessible: Mapped[Optional[int]]
    bikes_allowed: Mapped[Optional[int]]
    __table_args__ = (
        ForeignKeyConstraint(["id", "trip_id"], ["trip.id", "trip.trip_id"]),
        UniqueConstraint("transit_system_id", "trip_id", name="uq_trip"),
    )


class StopTime(ORMBase):
    """One row of stop_times.txt (one stop visit on one trip), materialized
    into the DB so /trip_detail's tail-stop backfill (see
    src/services/trip_detail.py's get_scheduled_tail_stops) can look it up
    with one indexed query instead of scanning the whole file - Helsinki's
    stop_times.txt alone is 7.8M rows/~700MB, which made that scan take a
    very long time per request. Populated by
    src/commands/fetcher.py's upsert_stop_times, via the same
    ON-CONFLICT-batched pattern as Route/Trip/Stop."""

    __tablename__ = "stop_time"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    transit_system_id: Mapped[int] = mapped_column(ForeignKey("transit_system.id"))
    trip_id: Mapped[str]
    stop_sequence: Mapped[int]
    stop_id: Mapped[str]

    __table_args__ = (
        # Also serves as the index get_scheduled_tail_stops queries against:
        # WHERE transit_system_id=? AND trip_id=? ORDER BY stop_sequence is
        # a prefix scan of this same constraint's btree index.
        UniqueConstraint(
            "transit_system_id", "trip_id", "stop_sequence", name="uq_stop_time"
        ),
    )


class Stop(ORMBase):
    __tablename__ = "stop"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    stop_id: Mapped[str] = mapped_column(primary_key=True, autoincrement=False)
    transit_system_id: Mapped[int] = mapped_column(ForeignKey("transit_system.id"))
    trip_id: Mapped[str]
    name: Mapped[str]
    # Optional per the GTFS spec (legacy zone-based fares) - many agencies
    # (e.g. Kiev) never populate it, so it can't be a hard requirement for a
    # stop to be usable.
    zone_id: Mapped[Optional[str]]
    stop_headsign: Mapped[Optional[str]]
    # Needed for any map display in a trip detail view - not required for a
    # stop to be "usable" (same reasoning as zone_id above), since the core
    # feature (stop_name) doesn't depend on it.
    lat: Mapped[Optional[float]]
    lon: Mapped[Optional[float]]
    platform_code: Mapped[Optional[str]]
    platform_name: Mapped[Optional[str]]
    wheelchair_boarding: Mapped[Optional[int]]
    __table_args__ = (
        UniqueConstraint("stop_id", "transit_system_id", name="uq_transit_stop_id"),
    )
