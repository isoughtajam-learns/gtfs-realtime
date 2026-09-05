"""
Pure derivation rules for turning raw GTFS Schedule rows into the values
src.commands.fetcher upserts - no file I/O, no DB access. Kept separate so
the fallback/completeness rules (which field wins, and why a row gets
dropped) are easy to find, read, and unit test without a real GTFS feed or
database.

Also used by fetcher.py's --diagnose mode to report exactly where a source
falls short of what these rules expect.
"""

from typing import Any, Dict, List, Optional, Tuple


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
    long_name: Optional[str],
    url: Optional[str],
    color: Optional[str],
    text_color: Optional[str],
) -> List[str]:
    """
    Every field here is optional per the GTFS spec, but our `route` table
    requires all of them - a row missing any one is dropped rather than
    inserted with a blank column. Returns the GTFS field names that are
    empty/missing; an empty list means the row is usable as-is.
    """
    candidates = {
        "route_id": route_id,
        "route_short_name": short_name,
        "route_long_name": long_name,
        "route_url": url,
        "route_color": color,
        "route_text_color": text_color,
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
