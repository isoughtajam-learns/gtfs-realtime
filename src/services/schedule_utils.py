"""
Pure derivation rules for turning raw GTFS Schedule rows into the values
src.commands.fetcher upserts - no file I/O, no DB access. Kept separate so
the fallback/completeness rules (which field wins, and why a row gets
dropped) are easy to find, read, and unit test without a real GTFS feed or
database.

Also used by fetcher.py's --diagnose mode to report exactly where a source
falls short of what these rules expect.
"""

from typing import List, Optional


def resolve_trip_headsign(
    trip_headsign: Optional[str],
    stop_time_headsign: Optional[str],
    route_long_name: Optional[str],
) -> Optional[str]:
    """
    Trip.name fallback chain, most to least authoritative:
      1. trips.txt's own trip_headsign - the agency's intended value.
      2. stop_times.txt's stop_headsign at the trip's earliest stop_sequence
         (see is_earlier_stop_sequence) - some agencies only set headsigns
         per-stop, not per-trip.
      3. routes.txt's route_long_name - a last-ditch "something is better
         than nothing" fallback so the UI never has to show a blank trip.
      4. None, if every source above is empty.
    """
    return trip_headsign or stop_time_headsign or route_long_name or None


def is_earlier_stop_sequence(
    candidate_seq: Optional[int], current_best_seq: Optional[int]
) -> bool:
    """
    Used while streaming stop_times.txt to track, per trip, the non-empty
    stop_headsign seen at the lowest stop_sequence so far (feeds
    resolve_trip_headsign's second fallback). Returns True if
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


def resolve_route_url(
    route_url: Optional[str], default_url: Optional[str]
) -> Optional[str]:
    """
    Route.url fallback: routes.txt's own route_url, else a per-transit-system
    default (constants.DEFAULT_SCHEDULE_URL_BY_SYSTEM) for agencies that
    don't publish a URL per route at all.
    """
    return route_url or default_url


def parse_direction_id(raw: Optional[str]) -> Optional[int]:
    """trips.txt's direction_id is an optional "0"/"1" string; blank or
    malformed values become None rather than failing the whole row."""
    if not raw:
        return None
    try:
        return int(raw)
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


def missing_stop_fields(
    trip_id: Optional[str], name: Optional[str], zone_id: Optional[str]
) -> List[str]:
    """
    A derived `stop` row needs a linked trip_id (from stop_times.txt), a
    stop_name, and a zone_id - all optional in raw GTFS, all required by
    our schema. Returns the missing field names; empty list means usable.
    """
    candidates = {
        "trip_id (via stop_times.txt)": trip_id,
        "stop_name": name,
        "zone_id": zone_id,
    }
    return [name for name, value in candidates.items() if not value]
