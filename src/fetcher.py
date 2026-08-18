"""
Fetcher is meant to check for updates to GTFS Schedule data for a transit system, parse the files, and store them for use.

Current scope:
trips.txt for trip_id and headsign
stops.txt for stop_id and stop_name
"""
import argparse
import csv
import io
import os
import re
import shutil
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import List

import requests
from sqlalchemy import select, Connection
from sqlalchemy.dialects.postgresql import insert

from src.database import engine
from src.constants import GTFS_URLS, GTFS_METADATA, \
    DEFAULT_SCHEDULE_URL_BY_SYSTEM, DISALLOWED_CHARS_PATTERN
from src.models import TransitSystem, Trip, Stop, Route


TMP_DIR = "src/tmp/"
METADATA_DIR = "src/metadata/"
METADATA_FILE = "feed_info.txt"
TRIPS_FILE = "trips.txt"
STOPS_FILE = "stops.txt"
STOP_TIMES_FILE = "stop_times.txt"
ROUTES_FILE = "routes.txt"
METADATA_FILE_LIST = [TRIPS_FILE, STOPS_FILE, STOP_TIMES_FILE, ROUTES_FILE]
UPDATED_METADATA_FILE = "updated_metadata.txt"

# Upsert rows in fixed-size batches rather than one statement per file, so a
# single very large source (more stops/trips/routes than this) can't build
# one enormous SQL statement or hold the whole insert payload at once.
UPSERT_BATCH_SIZE = 2000


def _upsert_batched(
    connection: Connection, table, rows: list[dict], constraint: str,
    conflict_columns: list[str], update_columns: list[str],
) -> int:
    # A single ON CONFLICT DO UPDATE statement can't touch the same row twice
    # (Postgres raises CardinalityViolation), so two source rows sharing a
    # conflict key can't land in the same batch. Some real feeds do this
    # legitimately - e.g. MBTA reuses generic route_short_names like "Red
    # Line Shuttle" across dozens of distinct route_ids. Dedupe up front,
    # keeping the last occurrence (matching normal upsert "last write wins"
    # semantics), rather than trying to keep duplicates apart across batches.
    deduped = {tuple(row[col] for col in conflict_columns): row for row in rows}
    rows = list(deduped.values())

    total = 0
    for start in range(0, len(rows), UPSERT_BATCH_SIZE):
        batch = rows[start:start + UPSERT_BATCH_SIZE]
        insert_stmt = insert(table).values(batch)
        upsert_stmt = insert_stmt.on_conflict_do_update(
            constraint=constraint,
            set_={col: getattr(insert_stmt.excluded, col) for col in update_columns},
        )
        total += connection.execute(upsert_stmt).rowcount
    return total


class Fetcher:
    url: str
    transit_system: str
    transit_system_id: int | None = None
    trips: dict[str, int] | None = None

    def __init__(self, url: str, transit_system: str) -> None:
        self.url = url
        self.transit_system = transit_system

    def tmp_dir(self) -> str:
        return f"{TMP_DIR}{self.transit_system}/"

    def real_dir(self) -> str:
        return f"{METADATA_DIR}{self.transit_system}/"

    def tmp_file(self, file_name: str):
        return f"{self.tmp_dir()}{file_name}"

    def real_file(self, file_name: str) -> str:
        return f"{self.real_dir()}{file_name}"

    def should_update(self) -> bool:
        files_exist = os.path.isfile(self.real_file(TRIPS_FILE)) and os.path.isfile(self.tmp_file(STOPS_FILE))
        metadata_exists = os.path.isfile(self.tmp_file(METADATA_FILE))
        if not metadata_exists:
            print(f"No feed_info.txt file found for GTFS Schedule - {self.transit_system}")
            return False
        with open(f"{self.tmp_dir()}{METADATA_FILE}", mode= "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                start = datetime.strptime(row.get("feed_start_date", "19700101"), "%Y%m%d")
                end = datetime.strptime(row.get("feed_end_date", "19700101"), "%Y%m%d")
                if not files_exist or not start <= datetime.today() <= end:
                    return True
        return False

    def update_metadata(self, file_names: List[str]) -> None:
        if not os.path.isdir(self.real_dir()):
            os.makedirs(self.real_dir())
        for file_name in file_names:
            tmp_file = Path(f"{self.tmp_file(file_name)}")
            real_file = Path(f"{self.real_file(file_name)}")
            tmp_file.rename(real_file)

    def push_updated_metadata_file(self) -> None:
        with open(self.real_file(UPDATED_METADATA_FILE), "w") as f:
            f.write(datetime.today().strftime("%Y%m%d"))

    def updated_recently(self):
        updated_metadata_file = self.real_file(UPDATED_METADATA_FILE)
        updated_file_exists = os.path.isfile(updated_metadata_file)
        if updated_file_exists:
            with open(updated_metadata_file, "r") as f:
                line = f.readline()
                if datetime.today().strftime("%Y%m%d") in line:
                    return True
        return False

    def fetched_today(self) -> bool:
        """DB-backed daily gate. `updated_recently()` above lives in the
        container's ephemeral filesystem and gets wiped on every
        restart/redeploy, so it can't reliably prevent repeat fetches across
        container churn - this checks TransitSystem.last_fetched_at instead,
        which survives restarts."""
        with engine.begin() as connection:
            last_fetched_at = connection.execute(
                select(TransitSystem.last_fetched_at).where(TransitSystem.name == self.transit_system)
            ).scalar()
        return last_fetched_at is not None and last_fetched_at.date() == datetime.utcnow().date()

    def remove_tmp(self) -> None:
        if os.path.isdir(self.tmp_dir()):
            shutil.rmtree(Path(self.tmp_dir()))

    def fetch_metadata_update(self, force: bool = False) -> None:
        if not force and (self.fetched_today() or self.updated_recently()):
            return

        if force:
            self.remove_tmp()

        response = requests.get(self.url, timeout=60)
        if response.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                z.extractall(f"{TMP_DIR}{self.transit_system}")

        if not force and not self.should_update():
            print(f"No updates needed from GTFS Schedule data - {self.transit_system}")
            return

        self.update_metadata(METADATA_FILE_LIST)
        self.push_updated_metadata_file()
        self.remove_tmp()
        self.do_upserts()

    def select_transit_system(self, connection: Connection) -> int | None:
        if self.transit_system_id:
            return self.transit_system_id

        results = connection.execute(
            select(TransitSystem).where(TransitSystem.name == self.transit_system)
        ).all()

        if results:
            self.transit_system_id = results[0].id
            return self.transit_system_id
        return None

    def do_upserts(self):
        with engine.begin() as connection:
            try:
                self.upsert_transit_system(connection)
                # stop_times.txt is by far the largest GTFS file (one row per
                # stop visit per trip) - scan it once and share the result,
                # rather than upsert_trips and upsert_stops each parsing it
                # independently. Reading it twice is what exhausted memory on
                # large sources (e.g. Helsinki) on a small instance.
                headsign_by_trip, stop_meta = self._scan_stop_times()
                self.upsert_trips(connection, headsign_by_trip)
                self.upsert_stops(connection, stop_meta)
                self.upsert_routes(connection)
            except Exception as ex:
                print(f"Error: {ex}")
        print(f"Upserts completed for {', '.join(METADATA_FILE_LIST)}")

    def upsert_transit_system(self, connection: Connection) -> None:
        realtime_url = GTFS_URLS.get(self.transit_system)
        schedule_url = GTFS_METADATA.get(self.transit_system)

        stmt = insert(TransitSystem).values(
            name=self.transit_system,
            realtime_url=realtime_url,
            schedule_url=schedule_url,
            last_fetched_at=datetime.utcnow(),
        )
        upsert_stmt = stmt.on_conflict_do_update(
            constraint="uq_name",
            set_=dict(
                realtime_url=stmt.excluded.realtime_url,
                schedule_url=stmt.excluded.schedule_url,
                last_fetched_at=stmt.excluded.last_fetched_at),
        )
        result = connection.execute(upsert_stmt)
        print(f"upsert transit system result: {result.keys()}")

    def upsert_routes(self, connection: Connection) -> None:
        transit_system_id = self.select_transit_system(connection)
        routes = []
        with open(self.real_file(ROUTES_FILE), "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                route_id = row.get("route_id")
                short_name = row.get("route_short_name")
                long_name = row.get("route_long_name")
                url = row.get("route_url") if row.get("route_url") else  DEFAULT_SCHEDULE_URL_BY_SYSTEM.get(self.transit_system)
                color = row.get("route_color")
                text_color = row.get("route_text_color")
                if not all([
                    route_id,
                    short_name,
                    long_name,
                    url,
                    color,
                    text_color
                ]):
                    continue
                routes.append({
                    "transit_system_id": transit_system_id,
                    "route_id": route_id,
                    "short_name": short_name,
                    "long_name": long_name,
                    "url": url,
                    "color": color,
                    "text_color": text_color,
                })
        if not routes:
            return
        total = _upsert_batched(
            connection, Route, routes, "uq_short_name", ["short_name"],
            ["short_name", "long_name", "url", "color", "text_color"],
        )
        print(f"upsert routes result: {total} rows")

    def _scan_stop_times(self) -> tuple[dict[str, str], dict[str, dict]]:
        """Single streaming pass over stop_times.txt, producing what both
        upsert_trips and upsert_stops need - previously each parsed this file
        independently, doubling memory/CPU on what is typically the largest
        GTFS file by far.

        Returns (headsign_by_trip, stop_meta):
          headsign_by_trip: trip_id -> earliest-stop_sequence non-empty stop_headsign.
          stop_meta: stop_id -> {"trip_id", "stop_headsign"} (last row wins,
            matching the original upsert_stops behavior).
        """
        headsign_by_trip: dict[str, str] = {}
        min_seq_by_trip: dict[str, int] = {}
        stop_meta: dict[str, dict] = {}
        with open(self.real_file(STOP_TIMES_FILE), "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row = dict([(re.sub(DISALLOWED_CHARS_PATTERN, "", k), v) for k, v in row.items()])
                trip_id = row.get("trip_id")
                headsign = row.get("stop_headsign")
                if trip_id and headsign:
                    try:
                        seq = int(row.get("stop_sequence", "0"))
                    except (TypeError, ValueError):
                        seq = None
                    if seq is not None and seq < min_seq_by_trip.get(trip_id, float("inf")):
                        min_seq_by_trip[trip_id] = seq
                        headsign_by_trip[trip_id] = headsign
                stop_id = row.get("stop_id")
                if stop_id:
                    stop_meta[stop_id] = {
                        "trip_id": trip_id,
                        "stop_headsign": headsign or None,
                    }
        return headsign_by_trip, stop_meta

    def _build_route_long_names(self) -> dict[str, str]:
        long_name_by_route: dict[str, str] = {}
        with open(self.real_file(ROUTES_FILE), "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                route_id = row.get("route_id")
                long_name = row.get("route_long_name")
                if route_id and long_name:
                    long_name_by_route[route_id] = long_name
        return long_name_by_route

    def upsert_trips(self, connection: Connection, stop_headsigns_by_trip: dict[str, str]) -> None:
        rows_to_write = []
        transit_system_id = self.select_transit_system(connection)
        route_long_names = self._build_route_long_names()

        with open(self.real_file(TRIPS_FILE), "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trip_id = row.get("trip_id")
                route_id = row.get("route_id")
                if not trip_id:
                    continue
                name = (
                    row.get("trip_headsign")
                    or stop_headsigns_by_trip.get(trip_id)
                    or route_long_names.get(route_id)
                    or None
                )
                direction_raw = row.get("direction_id")
                try:
                    direction_id = int(direction_raw) if direction_raw else None
                except (TypeError, ValueError):
                    direction_id = None
                rows_to_write.append({
                    "trip_id": trip_id,
                    "transit_system_id": transit_system_id,
                    "name": name,
                    "route_id": route_id,
                    "direction_id": direction_id,
                })
        if not rows_to_write:
            return
        total = _upsert_batched(
            connection, Trip, rows_to_write, "uq_trip", ["transit_system_id", "trip_id"],
            ["name", "direction_id"],
        )
        print(f"upsert trips result: {total} rows")

    def upsert_stops(self, connection: Connection, stop_meta: dict[str, dict]) -> None:
        transit_system_id = self.select_transit_system(connection)

        trip_stops = defaultdict(dict, stop_meta)
        with open(self.real_file(STOPS_FILE), "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                location_type = row.get('location_type')
                if location_type != "0":
                    continue
                stop_id = row.get('stop_id')

                if not stop_id:
                    continue
                trip_stops[stop_id].update({
                    "transit_system_id": transit_system_id,
                    "name": row.get('stop_name', ""),
                    "zone_id": row.get('zone_id', ""),
                })
        if not trip_stops:
            return

        rows_to_write = [dict(
            stop_id=k,
            transit_system_id=transit_system_id,
            trip_id=v.get("trip_id"),
            name=v.get("name"),
            zone_id=v.get("zone_id"),
            stop_headsign=v.get("stop_headsign"),
        ) for k, v in trip_stops.items() if all([
            v.get("trip_id"),
            v.get("name"),
            v.get("zone_id"),
        ])]

        total = _upsert_batched(
            connection, Stop, rows_to_write, "uq_transit_stop_id", ["stop_id", "transit_system_id"],
            ["name", "stop_headsign"],
        )
        print(f"upsert stops result: {total} rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch GTFS Schedule data and upsert it into the database."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run do_upserts() even if local schedule data is not stale.",
    )
    parser.add_argument(
        "--transit-system",
        default="BART",
        help="Name of transit system to fetch. Ignored if --all is passed.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch every transit system in GTFS_METADATA instead of a single one.",
    )
    args = parser.parse_args()

    systems = list(GTFS_METADATA.items()) if args.all else [(args.transit_system, GTFS_METADATA.get(args.transit_system))]
    for transit_system, schedule_url in systems:
        if not schedule_url:
            raise Exception("Invalid transit system: {}".format(transit_system))
        fetcher = Fetcher(schedule_url, transit_system)
        if args.all:
            # One slow/unreachable feed shouldn't block the rest, or the
            # startup command (fetcher.py && uvicorn) from ever reaching uvicorn.
            try:
                fetcher.fetch_metadata_update(force=args.force)
            except Exception as ex:
                print(f"Error fetching for {transit_system}: {ex}")
        else:
            fetcher.fetch_metadata_update(force=args.force)
