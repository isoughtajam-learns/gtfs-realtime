"""
Ingests the real station-complex subset of the regional (511.org
operator_id=RG) GTFS Schedule download's stop_areas.txt into StopArea -
IRL-21/IRL-12's investigation into whether the regional feed's
transfer-adjacent data is more useful than each individual feed's own
transfers.txt (see models.StopArea's docstring for the full finding).

Also builds RegionalStopMatch, a lat/lng-based crosswalk for the systems
(today: just BART - IRL-25) whose own stop_ids don't appear verbatim in
this feed, so StopArea membership alone can't resolve them. See
match_regional_stops() and models.RegionalStopMatch's docstring.

transfers.txt itself is untouched by this - that's still each system's own
Fetcher.upsert_transfers(). This is a separate, additive ingestion for data
that only exists in the combined regional feed.

Usage:
    API_KEY_511_ORG=... python -m src.commands.regional_stop_areas
"""

import argparse
import csv
import io
import logging
import math
import zipfile
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database import engine
from src.models import RegionalStopMatch, Stop, StopArea, TransitSystem
from src.services.schedule_utils import dedupe_rows_by_columns
from src.settings import get_settings
from src.telemetry import configure_posthog_logging

logger = logging.getLogger(__name__)

DATAFEEDS_URL = "http://api.511.org/transit/datafeeds"
STOP_AREAS_FILE = "stop_areas.txt"
STOPS_FILE = "stops.txt"
UPSERT_BATCH_SIZE = 2000
EARTH_RADIUS_METERS = 6_371_000.0
# Generous enough to cover a real station's multiple platforms/entrances
# (confirmed live: BART's own Millbrae platform and the regional feed's
# representation of it are well under this), narrow enough that it
# shouldn't accidentally match a genuinely different, merely nearby stop -
# see models.RegionalStopMatch's docstring and IRL-25.
MAX_MATCH_DISTANCE_METERS = 150.0

# Confirmed live: every real multi-agency station complex found (e.g.
# "mtc:powell", "mtc:daly-city-bart") uses this prefix and groups a handful
# of stops. Every GTFS Fare-V2 fare-zone definition re-using this same file
# (e.g. "AC:local", "SC:not-SJC", "ST:solano", "SO:zone:1".."7",
# "GG:San Francisco") and the literal "any" catch-all use some other prefix
# (or none) and span hundreds to tens of thousands of stops - a whole
# operator's service area, not a walkable interchange.
REAL_COMPLEX_PREFIX = "mtc:"
# Generous upper bound on a genuine walkable station complex's member count
# - every confirmed-real example had well under 10; every confirmed fare
# zone had hundreds at minimum. Exists as a defensive second check in case
# some future "mtc:"-prefixed area_id turns out to be a zone too.
MAX_REAL_COMPLEX_MEMBERS = 50


def is_real_station_complex(area_id: str, member_count: int) -> bool:
    return (
        area_id.startswith(REAL_COMPLEX_PREFIX)
        and area_id != REAL_COMPLEX_PREFIX
        and member_count <= MAX_REAL_COMPLEX_MEMBERS
    )


def download_regional_schedule_zip(api_key: str) -> bytes:
    response = requests.get(
        DATAFEEDS_URL,
        params={"api_key": api_key, "operator_id": "RG"},
        timeout=120,
    )
    response.raise_for_status()
    return response.content


def parse_stop_areas(zip_bytes: bytes) -> Dict[str, List[str]]:
    """area_id -> every stop_id listed under it in the raw file, unfiltered."""
    areas: Dict[str, List[str]] = defaultdict(list)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        with z.open(STOP_AREAS_FILE) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
            for row in reader:
                area_id, stop_id = row.get("area_id"), row.get("stop_id")
                if area_id and stop_id:
                    areas[area_id].append(stop_id)
    return areas


def filter_real_station_complexes(areas: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """Drops fare-zone buckets (see is_real_station_complex), returns the
    remaining area_id/stop_id pairs ready for upsert_stop_areas()."""
    rows: List[Dict[str, Any]] = []
    for area_id, member_stop_ids in areas.items():
        if not is_real_station_complex(area_id, len(member_stop_ids)):
            continue
        for stop_id in member_stop_ids:
            rows.append({"area_id": area_id, "stop_id": stop_id})
    return rows


def upsert_stop_areas(connection: Connection, rows: List[Dict[str, Any]]) -> int:
    """Insert-or-ignore: area_id/stop_id pairs are a fixed identity, not
    data that changes - nothing to update on a conflict, unlike every other
    upsert in this codebase (see _upsert_batched's docstring for why that
    one needs ON CONFLICT DO UPDATE instead)."""
    if not rows:
        return 0
    rows = dedupe_rows_by_columns(rows, ["area_id", "stop_id"])
    total = 0
    for start in range(0, len(rows), UPSERT_BATCH_SIZE):
        batch = rows[start : start + UPSERT_BATCH_SIZE]
        insert_stmt = pg_insert(StopArea).values(batch)
        upsert_stmt = insert_stmt.on_conflict_do_nothing(constraint="uq_stop_area")
        total += connection.execute(upsert_stmt).rowcount
    return total


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lng points, in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


def parse_stop_coordinates(
    zip_bytes: bytes, stop_ids: Set[str]
) -> Dict[str, Tuple[float, float]]:
    """regional stop_id -> (lat, lon), scoped to just the given stop_ids -
    station-complex members (see match_regional_stops), not every stop in
    the whole region. That's the only set a crosswalk could ever need to
    resolve, and the regional feed's stops.txt has tens of thousands of
    rows total."""
    coords: Dict[str, Tuple[float, float]] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        with z.open(STOPS_FILE) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
            for row in reader:
                stop_id = row.get("stop_id")
                if stop_id not in stop_ids:
                    continue
                lat, lon = row.get("stop_lat"), row.get("stop_lon")
                if lat and lon:
                    coords[stop_id] = (float(lat), float(lon))
    return coords


def match_regional_stops(
    connection: Connection, regional_stop_coords: Dict[str, Tuple[float, float]]
) -> List[Dict[str, Any]]:
    """For every regional stop_id in regional_stop_coords that doesn't
    already match an active system's own native stop_id verbatim (the
    normal StopArea/TransferCache path - true for 8 of 9 active systems,
    see models.RegionalStopMatch's docstring), find the nearest native stop
    across every active system within MAX_MATCH_DISTANCE_METERS and return
    it as a RegionalStopMatch row ready for upsert_regional_stop_matches().
    O(members x native stops) - fine at today's scale (a few hundred
    station-complex members, a few thousand native stops total), not
    something to run against the full regional feed."""
    native_stops = connection.execute(
        select(Stop.transit_system_id, Stop.stop_id, Stop.lat, Stop.lon)
        .select_from(Stop)
        .join(TransitSystem, TransitSystem.id == Stop.transit_system_id)
        .where(
            TransitSystem.active.is_(True),
            Stop.lat.is_not(None),
            Stop.lon.is_not(None),
        )
    ).all()
    native_stop_ids = {row.stop_id for row in native_stops}

    rows: List[Dict[str, Any]] = []
    for regional_stop_id, (lat, lon) in regional_stop_coords.items():
        if regional_stop_id in native_stop_ids:
            continue
        best: Optional[Tuple[int, str, float]] = None
        for native in native_stops:
            distance = _haversine_meters(lat, lon, native.lat, native.lon)
            if distance > MAX_MATCH_DISTANCE_METERS:
                continue
            if best is None or distance < best[2]:
                best = (native.transit_system_id, native.stop_id, distance)
        if best is not None:
            transit_system_id, stop_id, distance = best
            rows.append(
                {
                    "transit_system_id": transit_system_id,
                    "stop_id": stop_id,
                    "regional_stop_id": regional_stop_id,
                    "distance_meters": distance,
                }
            )
    return rows


def upsert_regional_stop_matches(
    connection: Connection, rows: List[Dict[str, Any]]
) -> int:
    """Insert-or-update, unlike upsert_stop_areas: a later run finding a
    closer/different match (upstream stops.txt or our own Stop data
    changed) should overwrite a stale one rather than being silently
    dropped."""
    if not rows:
        return 0
    rows = dedupe_rows_by_columns(rows, ["transit_system_id", "stop_id"])
    total = 0
    for start in range(0, len(rows), UPSERT_BATCH_SIZE):
        batch = rows[start : start + UPSERT_BATCH_SIZE]
        insert_stmt = pg_insert(RegionalStopMatch).values(batch)
        upsert_stmt = insert_stmt.on_conflict_do_update(
            constraint="uq_regional_stop_match",
            set_={
                "regional_stop_id": insert_stmt.excluded.regional_stop_id,
                "distance_meters": insert_stmt.excluded.distance_meters,
            },
        )
        total += connection.execute(upsert_stmt).rowcount
    return total


def main() -> None:
    api_key = get_settings().api_key_511_org
    if not api_key:
        raise SystemExit(
            "API_KEY_511_ORG is not set - export it before running this command."
        )

    logger.info("Downloading regional (RG) schedule zip...")
    zip_bytes = download_regional_schedule_zip(api_key)
    areas = parse_stop_areas(zip_bytes)
    rows = filter_real_station_complexes(areas)
    logger.info(
        f"{len(areas)} total area_ids in stop_areas.txt, "
        f"{len(rows)} rows kept as real station complexes"
    )

    with engine.begin() as connection:
        total = upsert_stop_areas(connection, rows)
    logger.info(f"upsert stop_areas result: {total} rows")

    member_stop_ids = {row["stop_id"] for row in rows}
    coords = parse_stop_coordinates(zip_bytes, member_stop_ids)
    with engine.begin() as connection:
        match_rows = match_regional_stops(connection, coords)
        match_total = upsert_regional_stop_matches(connection, match_rows)
    logger.info(
        f"{len(coords)} station-complex members had coordinates, "
        f"{len(match_rows)} needed a crosswalk match (not a direct stop_id "
        f"hit), upsert_regional_stop_matches result: {match_total} rows"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logger.setLevel(logging.DEBUG)
    configure_posthog_logging()
    argparse.ArgumentParser(description=__doc__).parse_args()
    main()
