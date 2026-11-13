"""
Pure derivation rules for turning raw GTFS Schedule rows into the values
src.commands.fetcher upserts - no file I/O, no DB access. Kept separate so
the fallback/completeness rules (which field wins, and why a row gets
dropped) are easy to find, read, and unit test without a real GTFS feed or
database.

Also used by fetcher.py's --diagnose mode to report exactly where a source
falls short of what these rules expect.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple


def dedupe_rows_by_columns(
    rows: List[Dict[str, Any]], columns: List[str]
) -> List[Dict[str, Any]]:
    """Deduplicates row-dicts by `columns`, keeping the last occurrence for
    each unique key - normal upsert "last write wins" semantics. Used
    before a batched ON CONFLICT DO UPDATE: Postgres raises
    CardinalityViolation if a single statement's VALUES would affect the
    same row twice, which happens whenever two source rows share the same
    conflict-target columns - a real, recurring case here, not a
    theoretical one: MBTA's routes.txt reuses the same route_short_name
    across many distinct route_ids for shuttle-bus replacements (e.g.
    "Rockport Line Shuttle" appears dozens of times)."""
    deduped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for row in rows:
        key = tuple(row[col] for col in columns)
        deduped[key] = row
    return list(deduped.values())


def resolve_trip_headsign(
    destination_stop_name: Optional[str],
    trip_headsign: Optional[str],
    stop_time_headsign: Optional[str],
    route_long_name: Optional[str],
) -> Optional[str]:
    """
    Trip.name fallback chain, most to least authoritative:
      1. The trip's own destination stop's real name (the stop at its
         highest stop_sequence, see is_later_stop_sequence) - reflects
         where the train actually terminates on this specific trip.
         trips.txt's own headsign can be wrong for an irregular run:
         confirmed live, BART's Saturday through-service trips carry a
         trip_headsign copied from an unrelated weekday route pattern
         (e.g. a Berryessa-to-Daly-City run labeled "OAK Airport / SF /
         Daly City"), even though the trip's own stop_times.txt sequence
         correctly shows where it really goes.
      2. trips.txt's own trip_headsign - used when this trip has no
         Schedule stop data of its own to derive a destination from.
      3. stop_times.txt's stop_headsign at the trip's earliest stop_sequence
         (see is_earlier_stop_sequence) - some agencies only set headsigns
         per-stop, not per-trip.
      4. routes.txt's route_long_name - a last-ditch "something is better
         than nothing" fallback so the UI never has to show a blank trip.
      5. None, if every source above is empty.
    """
    return (
        destination_stop_name
        or trip_headsign
        or stop_time_headsign
        or route_long_name
        or None
    )


def is_earlier_stop_sequence(
    candidate_seq: Optional[int], current_best_seq: Optional[int]
) -> bool:
    """
    Used while streaming stop_times.txt to track, per trip, the non-empty
    stop_headsign seen at the lowest stop_sequence so far (feeds
    resolve_trip_headsign's third fallback). Returns True if
    `candidate_seq` should replace `current_best_seq` as the new best.
    GTFS agencies often only populate stop_headsign on a trip's first
    stop_time, so the earliest sequence number is the one most likely to
    actually be set.
    """
    if candidate_seq is None:
        return False
    if current_best_seq is None:
        return True
    return candidate_seq < current_best_seq


def is_later_stop_sequence(
    candidate_seq: Optional[int], current_best_seq: Optional[int]
) -> bool:
    """
    Used while streaming stop_times.txt to track, per trip, the stop_id at
    the highest stop_sequence seen so far - the trip's actual destination
    stop (feeds resolve_trip_headsign's first, highest-priority fallback).
    Returns True if `candidate_seq` should replace `current_best_seq` as
    the new best. Where a train actually stops is never wrong, unlike
    trips.txt's own trip_headsign field (see resolve_trip_headsign).
    """
    if candidate_seq is None:
        return False
    if current_best_seq is None:
        return True
    return candidate_seq > current_best_seq


def resolve_route_url(
    route_url: Optional[str], default_url: Optional[str]
) -> Optional[str]:
    """
    Route.url fallback: routes.txt's own route_url, else a per-transit-system
    default (models.TransitSystem.default_schedule_url) for agencies that
    don't publish a URL per route at all.
    """
    return route_url or default_url


def parse_optional_int(raw: Optional[str]) -> Optional[int]:
    """Generic parser for the many small GTFS integer enums/codes that are
    optional in practice (direction_id, route_type, wheelchair_accessible,
    bikes_allowed, wheelchair_boarding, ...): blank or malformed values
    become None rather than failing the whole row."""
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_optional_float(raw: Optional[str]) -> Optional[float]:
    """stops.txt's stop_lat/stop_lon are technically required by the GTFS
    spec, but blank or malformed values show up in practice; treat them
    the same permissive way as every other optional-in-practice field
    here rather than failing the whole row."""
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def missing_route_fields(
    route_id: Optional[str],
    short_name: Optional[str],
) -> List[str]:
    """
    route_id and route_short_name are the only fields a route row actually
    needs to be usable: route_id for identity/joins, and short_name because
    it's part of our table's primary key. long_name/url/color/text_color are
    all genuinely optional per the GTFS spec - requiring them used to drop
    every usable route from a source that simply doesn't populate one of
    them (confirmed live: SamTrans has no route_color/route_text_color at
    all - 0/75 routes usable; Caltrain only sets route_long_name on 1 of its
    5 routes). Returns the GTFS field names that are empty/missing; an empty
    list means the row is usable as-is.
    """
    candidates = {
        "route_id": route_id,
        "route_short_name": short_name,
    }
    return [name for name, value in candidates.items() if not value]


def missing_stop_fields(trip_id: Optional[str], name: Optional[str]) -> List[str]:
    """
    A derived `stop` row needs a linked trip_id (from stop_times.txt) and a
    stop_name to be usable - these are the fields the core "show stop_name
    on the map" feature depends on. zone_id is deliberately excluded: it's
    optional in the GTFS spec (legacy zone-based fares) and many agencies
    (e.g. Kiev) never populate it at all, so requiring it would make the
    whole stop unusable over a field nothing downstream depends on - the
    same "partial data is still useful" reasoning that keeps a
    route_color-less Helsinki enabled. Returns the missing field names;
    empty list means usable.
    """
    candidates = {
        "trip_id (via stop_times.txt)": trip_id,
        "stop_name": name,
    }
    return [name for name, value in candidates.items() if not value]


DAY_SECONDS = 86400


def parse_gtfs_time_to_seconds(raw: Optional[str]) -> Optional[int]:
    """Parses a stop_times.txt arrival_time/departure_time ("HH:MM:SS") into
    seconds since midnight of its trip's service day. Hours routinely exceed
    24 for a trip running past midnight - GTFS attributes that trip to the
    *previous* day's service_id rather than rolling the clock over, so this
    deliberately does NOT wrap mod 24h: is_outside_operating_hours below
    depends on telling a 25:30:00 trip apart from a 01:30:00 one. Blank,
    missing, or malformed values (legal for interpolated/non-timepoint
    stops) become None rather than failing the row."""
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


@dataclass(frozen=True)
class CalendarRow:
    """Plain, DB-independent mirror of one models.Calendar row - weekdays is
    (monday, tuesday, ..., sunday), matching date.weekday()'s Monday=0
    ordering."""

    service_id: str
    weekdays: Tuple[bool, bool, bool, bool, bool, bool, bool]
    start_date: date
    end_date: date


@dataclass(frozen=True)
class CalendarDateRow:
    """Plain, DB-independent mirror of one models.CalendarDate row."""

    service_id: str
    date: date
    exception_type: int  # GTFS enum: 1 = added, 2 = removed


def resolve_active_service_ids(
    calendar_rows: List[CalendarRow],
    calendar_date_rows: List[CalendarDateRow],
    target_date: date,
) -> Set[str]:
    """Which service_ids actually run on `target_date`: Calendar's day-of-
    week rule (within its start/end date range) as the base set, then
    CalendarDate's one-off exceptions for that exact date layered on top
    (exception_type=1 adds a service_id even if Calendar didn't already
    include it - the only way service ever gets defined at all for a feed
    with no calendar.txt; exception_type=2 removes one Calendar said should
    run, e.g. a holiday)."""
    weekday_index = target_date.weekday()
    active = {
        row.service_id
        for row in calendar_rows
        if row.weekdays[weekday_index] and row.start_date <= target_date <= row.end_date
    }
    for exception in calendar_date_rows:
        if exception.date != target_date:
            continue
        if exception.exception_type == 1:
            active.add(exception.service_id)
        elif exception.exception_type == 2:
            active.discard(exception.service_id)
    return active


@dataclass(frozen=True)
class OperatingHoursResult:
    outside_operating_hours: bool
    next_service_starts_at: Optional[datetime] = None


def is_outside_operating_hours(
    now_local: datetime,
    windows_by_service_id: Dict[str, List[Tuple[int, int]]],
    calendar_rows: List[CalendarRow],
    calendar_date_rows: List[CalendarDateRow],
) -> OperatingHoursResult:
    """Whether `now_local` (a timezone-aware datetime in the transit
    system's own local timezone) falls outside every currently-active
    service_id's scheduled window. `windows_by_service_id` is
    service_id -> list of per-*trip* (min_departure_seconds,
    max_arrival_seconds) windows, aggregated in SQL by ScheduleCache -
    never raw stop_time rows (see that module's docstring on Helsinki's
    7.8M-row table). Deliberately per-trip, not one merged span per
    service_id: a service_id with an early-morning trip and a separate
    late-night trip (nothing in between) must not read as "in service"
    for the whole span, including the midday gap when nothing is actually
    running - exactly the shape a low-frequency system like a ferry can
    have.

    Checks both today's and *yesterday's* active service_ids: GTFS
    routinely represents a trip past midnight with times >= 24:00:00,
    still attributed to the previous day's service_id, so a late-running
    trip from yesterday can still be "in service" right now even though
    today's own service_ids haven't started yet. Yesterday's window is
    compared by shifting `now_local`'s seconds-since-midnight forward by a
    full day, putting it on the same footing as yesterday's >=24:00:00
    times.

    Fails open (returns outside_operating_hours=False) in two cases,
    both "unknown" rather than "closed": no calendar data at all for this
    system (same precedent as every other optional-GTFS-field gap in this
    codebase - Helsinki's missing route colors, Kiev's missing zone_id),
    or calendar data says a service_id should be active but
    `windows_by_service_id` has no timing data for *any* currently-active
    service_id (today's or yesterday's) - e.g. right after this feature's
    migration ships, before stop_time's new time columns have been
    backfilled by a fresh fetch. A real GTFS gap (data present, service
    genuinely not running right now) is unaffected: that case has at
    least one active service_id's window data to check against."""
    if not calendar_rows and not calendar_date_rows:
        return OperatingHoursResult(outside_operating_hours=False)

    today = now_local.date()
    yesterday = today - timedelta(days=1)
    seconds_today = now_local.hour * 3600 + now_local.minute * 60 + now_local.second

    today_services = resolve_active_service_ids(
        calendar_rows, calendar_date_rows, today
    )
    yesterday_services = resolve_active_service_ids(
        calendar_rows, calendar_date_rows, yesterday
    )

    known_services = (
        today_services | yesterday_services
    ) & windows_by_service_id.keys()
    if not known_services:
        return OperatingHoursResult(outside_operating_hours=False)

    in_service = _time_in_any_window(
        seconds_today, today_services, windows_by_service_id
    )

    if not in_service:
        in_service = _time_in_any_window(
            seconds_today + DAY_SECONDS, yesterday_services, windows_by_service_id
        )

    if in_service:
        return OperatingHoursResult(outside_operating_hours=False)

    return OperatingHoursResult(
        outside_operating_hours=True,
        next_service_starts_at=_next_service_start(
            now_local, calendar_rows, calendar_date_rows, windows_by_service_id
        ),
    )


def _time_in_any_window(
    seconds: int,
    service_ids: Set[str],
    windows_by_service_id: Dict[str, List[Tuple[int, int]]],
) -> bool:
    for service_id in service_ids:
        for start, end in windows_by_service_id.get(service_id, []):
            if start <= seconds <= end:
                return True
    return False


def _next_service_start(
    now_local: datetime,
    calendar_rows: List[CalendarRow],
    calendar_date_rows: List[CalendarDateRow],
    windows_by_service_id: Dict[str, List[Tuple[int, int]]],
) -> Optional[datetime]:
    """Best-effort only, per is_outside_operating_hours's docstring - checks
    just today's remaining active service and tomorrow's, returning the
    earliest still-upcoming departure found (across every trip window of
    every active service_id); None if neither has one (e.g. no calendar
    data for tomorrow yet, or a system with irregular/rare service this
    function isn't meant to search arbitrarily far for)."""
    today = now_local.date()
    seconds_now = now_local.hour * 3600 + now_local.minute * 60 + now_local.second

    for days_ahead, service_date in ((0, today), (1, today + timedelta(days=1))):
        service_ids = resolve_active_service_ids(
            calendar_rows, calendar_date_rows, service_date
        )
        starts = [
            window[0]
            for service_id in service_ids
            for window in windows_by_service_id.get(service_id, [])
        ]
        if days_ahead == 0:
            starts = [start for start in starts if start > seconds_now]
        if not starts:
            continue
        service_midnight = datetime.combine(
            service_date, datetime.min.time(), tzinfo=now_local.tzinfo
        )
        return service_midnight + timedelta(seconds=min(starts))
    return None
