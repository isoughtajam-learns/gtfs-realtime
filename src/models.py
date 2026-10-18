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
    # The raw GTFS route_id this trip belongs to - not shown directly, but
    # needed client-side to tell whether route_short_name is a genuine
    # rider-facing route identifier (route_short_name == route_id, e.g. AC
    # Transit's "72") or an internal grouping code that doesn't match any
    # route_id (BART's "Yellow-N" vs route_id "2") - see IRL-13/IRL-22.
    # Trip-then-route fallback already applied server-side (see main.py),
    # same as route_short_name/route_long_name below, so a source that
    # leaves the live entity's own route_id blank (confirmed live: BART)
    # still gets the correct value here.
    route_id: Optional[str] = None
    # Real-world signage/colloquial route naming (e.g. SF Muni's "2", "48",
    # "14R") - distinct from trip_headsign, which is a destination
    # description, not a line identifier. Confirmed live: SF-MTA's own
    # service alerts refer to routes this way ("48 RRT STOP CLOSED"), so
    # without this a live event and the alert it relates to shared no
    # visible connection. See src/services/schedule_cache.py.
    route_short_name: Optional[str] = None
    # The full colloquial route name (e.g. SF Muni's "TARAVAL", paired with
    # route_short_name "L" to read as "L Taraval" the way riders/signage do)
    # - distinct from trip_headsign for the same reason route_short_name is.
    route_long_name: Optional[str] = None
    color: Optional[str] = None
    text_color: Optional[str] = None
    # GTFS-RT trip_update.timestamp ("last measured" time for this vehicle),
    # falling back to the feed header's timestamp when a source doesn't set
    # the per-trip one. Ranking key for RecentEventsCache - see
    # src/services/recent_events_cache.py - not otherwise used/displayed.
    timestamp: Optional[int] = None


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


class AlertActivePeriod(BaseModel):
    """One start/end window a ServiceAlert is in effect - GTFS-RT allows
    several per alert (e.g. recurring weekend maintenance). Either bound
    can be unset (open-ended)."""

    start: Optional[int] = None
    end: Optional[int] = None


class AffectedTrip(BaseModel):
    """One trip a ServiceAlert names, either directly (informed_entity.trip)
    or via one of its stops - hydrated from our own stored Trip row, not
    whatever the alert entity itself happened to set (which BART's live
    feed frequently leaves blank for route_id - see trip_detail.py's same
    reasoning for route_id)."""

    trip_id: str
    trip_headsign: Optional[str] = None
    route_id: Optional[str] = None


class AffectedRoute(BaseModel):
    """One route a ServiceAlert names - either directly (informed_entity.
    route_id) or via an affected trip's own route_id."""

    route_id: str
    route_short_name: Optional[str] = None
    route_long_name: Optional[str] = None


class ServiceAlert(BaseModel):
    """One GTFS-RT Alert entity, hydrated against our stored Schedule data.
    Response shape for GET /service_alerts/{transit_system} - see
    src/services/service_alerts.py."""

    alert_id: str
    cause: str
    effect: str
    severity_level: Optional[str] = None
    header_text: Optional[str] = None
    description_text: Optional[str] = None
    url: Optional[str] = None
    active_period: list[AlertActivePeriod] = []
    affected_trips: list[AffectedTrip] = []
    affected_routes: list[AffectedRoute] = []


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
    # Unix timestamp of this system's next scheduled TripUpdates refresh -
    # see src/services/quota_group_scheduler.py. None for a system not in
    # any scheduled quota_group (fetched purely on demand instead, with no
    # predictable cadence to expose). Unlike every other field here, this
    # one changes constantly (recomputed on nearly every real fetch) - a
    # deliberate exception to this endpoint's usual "changes rarely"
    # contract, since it's meant to back a countdown-to-next-update UI.
    next_realtime_update_at: Optional[int] = None


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
    # Minimum seconds between real outbound polls of realtime_url for this
    # system, shared across every caller (main.py's SSE loop - one poll per
    # connected client - and /trip_detail, which previously fetched fresh
    # on every single request with no throttling at all). 0 (the default)
    # means never cache - matches pre-rate-limiting behavior exactly for
    # every system that doesn't need this. Exists because some sources
    # enforce a strict per-key quota (e.g. 511.org) that concurrent
    # uncoordinated polling could exceed - see
    # src/services/realtime_feed_cache.py.
    min_poll_interval_seconds: Mapped[int] = mapped_column(
        default=0, server_default="0"
    )
    # GTFS-RT ServiceAlerts feed URL - a distinct feed from realtime_url
    # (TripUpdates), usually a sibling endpoint (confirmed live for BART,
    # SF-MTA/511.org). Optional: not every system has one, or we may not
    # have found it yet. Polled through the same RealtimeFeedCache/
    # min_poll_interval_seconds as realtime_url (see
    # src/services/service_alerts.py) - a system with a strict per-key
    # quota (511.org) has that quota shared across both feeds, not budgeted
    # separately.
    alerts_url: Mapped[Optional[str]]
    # Name of a shared outbound-request budget this system's realtime_url/
    # alerts_url fetches draw from, alongside every other TransitSystem row
    # with the same value here - see src/services/shared_feed_quota.py.
    # Distinct from min_poll_interval_seconds: that throttles this one
    # system's own feed in isolation, but has no way to know about a quota
    # actually shared across several *different* systems (confirmed live:
    # every 511.org-backed system draws on the same 60-requests/hour
    # API-key budget, not one each). None (the default) means this
    # system's fetches aren't coordinated with anyone else's - true for
    # every system with its own dedicated source/key.
    quota_group: Mapped[Optional[str]]
    # This system's 511.org operator code (e.g. "SF", "AC", "CT" - see
    # IRL-7's findings), when it's fed by the combined agency=RG regional
    # fetch instead of its own direct realtime_url/alerts_url request - see
    # src/services/regional_feed.py. None (the default) means this system
    # isn't regional-fed: realtime_url/alerts_url stay its own direct
    # fetch, same as before this column existed (every system today, and
    # any future one with its own dedicated source/key - e.g. BART, which
    # 511.org also lists but which already has its own standalone feed and
    # is deliberately NOT being migrated to this - see IRL-7).
    realtime_feed_agency_code: Mapped[Optional[str]]
    __table_args__ = (UniqueConstraint("name", name="uq_name"),)


class Route(ORMBase):
    __tablename__ = "route"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    transit_system_id: Mapped[int] = mapped_column(ForeignKey("transit_system.id"))
    route_id: Mapped[str]
    # NOT globally unique - see uq_short_name below. Composite PK with `id`
    # kept as-is (not the source of the bug this comment is about), even
    # though `id` alone already uniquely identifies every row.
    short_name: Mapped[str] = mapped_column(primary_key=True, autoincrement=False)
    # All four Optional per the GTFS spec - route_short_name/route_long_name
    # only require *one* of the pair (short_name carries that requirement by
    # staying non-nullable; long_name doesn't need to), and route_color/
    # route_text_color/route_url are purely cosmetic. Confirmed live: making
    # these required dropped every one of SamTrans's 75 routes (no colors in
    # its routes.txt) and 4 of Caltrain's 5 (no long_name) - see
    # missing_route_fields().
    long_name: Mapped[Optional[str]]
    url: Mapped[Optional[str]]
    color: Mapped[Optional[str]]
    text_color: Mapped[Optional[str]]
    # GTFS numeric mode enum (0=tram, 1=subway, 2=rail, 3=bus, 4=ferry, ...) -
    # optional here for the same reason zone_id is: not every field we'd
    # like to show needs to gate whether a route is usable at all.
    route_type: Mapped[Optional[int]]

    __table_args__ = (
        UniqueConstraint("id", "short_name", name="uq_route_short_name"),
        # Scoped per transit_system, NOT globally unique across every
        # system - it used to be global, deliberately, to dedupe a single
        # agency's own route_short_name reuse (confirmed live: MBTA's
        # routes.txt reuses one short_name across many distinct route_ids
        # for shuttle-bus replacements). That reasoning never required
        # global scope, and global scope turned out to actively corrupt
        # data once multiple real Bay Area agencies went live together:
        # confirmed live, 67 of 310 distinct short_name values collide
        # across 2-4 different systems (plain numeric bus route codes like
        # "22"/"52"/"F" are common across agencies) - _upsert_batched's
        # ON CONFLICT (short_name) DO UPDATE doesn't update transit_system_id,
        # so whichever system claimed a given short_name first kept
        # permanent ownership of that row, and every other system's own
        # same-numbered route silently vanished (upserted into the wrong
        # system's row, visible as route_short_name/long_name/color all
        # None for that system's own trips).
        UniqueConstraint("transit_system_id", "short_name", name="uq_short_name"),
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


class Transfer(ORMBase):
    """GTFS transfers.txt records describing a transfer relationship between
    two stops, optionally scoped to specific routes/trips.
    See https://gtfs.org/schedule/reference/#transferstxt

    transfers.txt has no unique "transfer_id" field in the real GTFS spec -
    an earlier version of this model invented one, along with several other
    non-standard column names (from_stop_code/to_stop_code/transit_time/
    destination_route_id), none of which exist in any real-world feed; every
    row was silently dropped on ingestion as a result. Columns here match
    the actual spec:
      from_stop_id/to_stop_id - stops this rule connects (required)
      from_route_id/to_route_id - optionally scopes the rule to one route
        rather than every route serving from_stop_id/to_stop_id
      from_trip_id/to_trip_id - optionally scopes the rule to one trip
        (required by the spec for transfer_type 4/5, in-seat transfers)
      transfer_type - GTFS enum: 0/blank=recommended, 1=timed (departing
        vehicle waits), 2=requires min_transfer_time, 3=not possible,
        4=in-seat transfer, 5=in-seat transfer not allowed
      min_transfer_time - seconds needed, only meaningful for transfer_type=2
    """

    __tablename__ = "transfer"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    transit_system_id: Mapped[int] = mapped_column(ForeignKey("transit_system.id"))
    from_stop_id: Mapped[str]
    to_stop_id: Mapped[str]
    from_route_id: Mapped[Optional[str]]
    to_route_id: Mapped[Optional[str]]
    from_trip_id: Mapped[Optional[str]]
    to_trip_id: Mapped[Optional[str]]
    transfer_type: Mapped[Optional[int]]
    min_transfer_time: Mapped[Optional[int]]

    __table_args__ = (
        # Keyed on the stop pair alone, not the full GTFS identity tuple
        # (which would also include from_route_id/to_route_id/from_trip_id/
        # to_trip_id) - Postgres treats NULLs as distinct for uniqueness
        # purposes, so a compound key including those mostly-blank optional
        # columns would let literal duplicate rows (same stop pair, same
        # blank route/trip scoping) insert as separate rows instead of
        # deduping, the same real failure mode _upsert_batched's docstring
        # already describes for MBTA's route_short_name reuse.
        UniqueConstraint(
            "transit_system_id", "from_stop_id", "to_stop_id", name="uq_transfer"
        ),
    )


class StopArea(ORMBase):
    """Regional (MTC-curated) station-complex groupings, from the combined
    511.org operator_id=RG schedule's stop_areas.txt - IRL-21/IRL-12's
    investigation into whether the regional feed's transfer-adjacent data is
    more useful than each individual feed's own transfers.txt.

    Confirmed live: the regional transfers.txt itself is NOT an upgrade -
    only 53 rows system-wide, and BART's own transfers.txt alone already
    contains 40 of those same rows verbatim (just with agency-prefixed
    route_ids) - zero genuine cross-agency transfer rows. transfers.txt
    ingestion (see Transfer/upsert_transfers()) is unchanged by this.

    stop_areas.txt is different: a real subset of its area_id groupings are
    genuine multi-agency station complexes no individual feed could ever
    represent (e.g. "mtc:powell" groups BART's Powell St platform stop_ids
    with SF-MTA's nearby Muni stop_ids). But the file conflates this with
    GTFS Fare-V2 fare-zone definitions re-using the same area_id/stop_id
    shape (this feed also publishes fare_leg_rules.txt/fare_products.txt) -
    confirmed live: area_ids like "AC:local" (4,675 stops), "SC:not-SJC"
    (3,301), "ST:solano", "SO:zone:1"-"7", "GG:San Francisco", and a literal
    "any" catch-all (21,632 stops) are fare zones spanning an entire
    operator's service area, not walkable interchanges. Every real station
    complex found was "mtc:"-prefixed and small (single digits of member
    stops); every fare zone used some other prefix (or none) and was large.
    See upsert_stop_areas() for the exact filter.

    Deliberately NOT scoped to a transit_system_id (unlike every other table
    here) - an area's whole value is spanning multiple agencies' own
    stop_id namespaces at once. stop_id is stored exactly as the regional
    feed gives it; matching it to a specific system's own Stop row (they
    share the same raw stop_id values - confirmed live against BART's own
    stops.txt) is left to the caller.
    """

    __tablename__ = "stop_area"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    area_id: Mapped[str]
    stop_id: Mapped[str]

    __table_args__ = (UniqueConstraint("area_id", "stop_id", name="uq_stop_area"),)
