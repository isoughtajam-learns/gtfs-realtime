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
import shutil
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, List

import requests
from sqlalchemy import select, Connection
from sqlalchemy.dialects.postgresql import insert

from src.database import engine
from src.constants import GTFS_URLS, GTFS_METADATA, DEFAULT_SCHEDULE_URL_BY_SYSTEM
from src.models import ORMBase, TransitSystem, Trip, Stop, Route
from src.services.schedule_utils import (
    is_earlier_stop_sequence,
    missing_route_fields,
    missing_stop_fields,
    parse_direction_id,
    resolve_route_url,
    resolve_trip_headsign,
)


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
    connection: Connection,
    table: type[ORMBase],
    rows: list[dict[str, Any]],
    constraint: str,
    update_columns: list[str],
) -> int:
    total = 0
    for start in range(0, len(rows), UPSERT_BATCH_SIZE):
        batch = rows[start : start + UPSERT_BATCH_SIZE]
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

    def tmp_file(self, file_name: str) -> str:
        return f"{self.tmp_dir()}{file_name}"

    def real_file(self, file_name: str) -> str:
        return f"{self.real_dir()}{file_name}"

    def should_update(self) -> bool:
        files_exist = os.path.isfile(self.real_file(TRIPS_FILE)) and os.path.isfile(
            self.tmp_file(STOPS_FILE)
        )
        metadata_exists = os.path.isfile(self.tmp_file(METADATA_FILE))
        if not metadata_exists:
            print(
                f"No feed_info.txt file found for GTFS Schedule - {self.transit_system}"
            )
            return False
        with open(
            f"{self.tmp_dir()}{METADATA_FILE}",
            mode="r",
            newline="",
            encoding="utf-8-sig",
        ) as f:
            reader = csv.DictReader(f)
            for row in reader:
                start = datetime.strptime(
                    row.get("feed_start_date", "19700101"), "%Y%m%d"
                )
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

    def updated_recently(self) -> bool:
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
                select(TransitSystem.last_fetched_at).where(
                    TransitSystem.name == self.transit_system
                )
            ).scalar()
        return (
            last_fetched_at is not None
            and last_fetched_at.date() == datetime.utcnow().date()
        )

    def remove_tmp(self) -> None:
        if os.path.isdir(self.tmp_dir()):
            shutil.rmtree(Path(self.tmp_dir()))

    def diagnose_only(self) -> dict[str, Any]:
        """Standalone entry point for `--diagnose`: downloads the feed into
        tmp/, reports on it, and cleans up - never promotes into real_dir()
        and never touches the database. Use this to check a system before
        adding it to GTFS_METADATA (see README's "Before enabling a new
        large transit system" section) - fetch_metadata_update() runs the
        same diagnosis automatically as a gate on every real fetch, this is
        just for checking ahead of time."""
        self.remove_tmp()
        response = requests.get(self.url, timeout=60)
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            z.extractall(self.tmp_dir())
        headsign_by_trip, stop_meta, stop_times_rows = self._scan_stop_times(
            self.tmp_file(STOP_TIMES_FILE)
        )
        report = self.diagnose(headsign_by_trip, stop_meta, stop_times_rows)
        self.remove_tmp()
        return report

    def diagnose(
        self,
        headsign_by_trip: dict[str, str],
        stop_meta: dict[str, dict[str, Any]],
        stop_times_row_count: int,
    ) -> dict[str, Any]:
        """Analyzes the GTFS Schedule files currently sitting in tmp_dir()
        and reports how well they'll satisfy upsert_trips/upsert_stops/
        upsert_routes - i.e. whether this source has the data we expect,
        and if not, exactly where it falls short. Read-only: doesn't touch
        the database or real_dir(). Always prints; also returns the report
        dict for programmatic use (see _diagnosis_passes)."""
        report = {
            "transit_system": self.transit_system,
            "feed_info": self._diagnose_feed_info(),
            "stop_times": {"row_count": stop_times_row_count},
            "routes": self._diagnose_routes(),
            "trips": self._diagnose_trips(headsign_by_trip),
            "stops": self._diagnose_stops(stop_meta),
        }
        self._print_diagnosis(report)
        return report

    def _diagnosis_passes(self, report: dict[str, Any]) -> bool:
        """Minimum bar for promoting tmp/ into real_dir() and upserting:
        this source must be able to produce at least one complete stop, or
        the app's core feature (showing stop_name on the map) is entirely
        unavailable for it. Missing route colors, unresolved trip
        headsigns, or a large stop_times.txt are reported but don't block
        ingestion on their own - partial data is still useful (e.g.
        Helsinki has no route colors and is still enabled)."""
        stops = report["stops"]
        return bool(stops["present"] and stops["usable_rows"] > 0)

    def _diagnose_feed_info(self) -> dict[str, Any]:
        path = self.tmp_file(METADATA_FILE)
        if not os.path.isfile(path):
            return {"present": False}
        with open(path, mode="r", newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return {"present": True, "valid_today": False}
        row = rows[0]
        start = datetime.strptime(row.get("feed_start_date", "19700101"), "%Y%m%d")
        end = datetime.strptime(row.get("feed_end_date", "19700101"), "%Y%m%d")
        return {
            "present": True,
            "valid_today": start <= datetime.today() <= end,
            "feed_start_date": row.get("feed_start_date"),
            "feed_end_date": row.get("feed_end_date"),
        }

    def _diagnose_routes(self) -> dict[str, Any]:
        path = self.tmp_file(ROUTES_FILE)
        if not os.path.isfile(path):
            return {"present": False}
        total = 0
        usable = 0
        missing_counts: dict[str, int] = defaultdict(int)
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total += 1
                url = resolve_route_url(
                    row.get("route_url"),
                    DEFAULT_SCHEDULE_URL_BY_SYSTEM.get(self.transit_system),
                )
                missing = missing_route_fields(
                    row.get("route_id"),
                    row.get("route_short_name"),
                    row.get("route_long_name"),
                    url,
                    row.get("route_color"),
                    row.get("route_text_color"),
                )
                if missing:
                    for field in missing:
                        missing_counts[field] += 1
                else:
                    usable += 1
        return {
            "present": True,
            "total_rows": total,
            "usable_rows": usable,
            "missing_field_counts": dict(missing_counts),
        }

    def _diagnose_trips(self, headsign_by_trip: dict[str, str]) -> dict[str, Any]:
        path = self.tmp_file(TRIPS_FILE)
        if not os.path.isfile(path):
            return {"present": False}
        route_long_names = self._build_route_long_names(self.tmp_file(ROUTES_FILE))
        total = 0
        resolved_by_source: dict[str, int] = defaultdict(int)
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total += 1
                trip_id = row.get("trip_id")
                if not trip_id:
                    continue
                direct = row.get("trip_headsign")
                via_stop_time = headsign_by_trip.get(trip_id)
                route_id = row.get("route_id")
                via_route = route_long_names.get(route_id) if route_id else None
                if direct:
                    resolved_by_source["trip_headsign"] += 1
                elif via_stop_time:
                    resolved_by_source["stop_times.stop_headsign"] += 1
                elif via_route:
                    resolved_by_source["routes.route_long_name"] += 1
                else:
                    resolved_by_source["unresolved"] += 1
        return {
            "present": True,
            "total_rows": total,
            "headsign_resolved_by": dict(resolved_by_source),
        }

    def _diagnose_stops(self, stop_meta: dict[str, dict[str, Any]]) -> dict[str, Any]:
        path = self.tmp_file(STOPS_FILE)
        if not os.path.isfile(path):
            return {"present": False}
        trip_stops = defaultdict(dict, stop_meta)
        location_type_0_rows = 0
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("location_type") != "0":
                    continue
                location_type_0_rows += 1
                stop_id = row.get("stop_id")
                if not stop_id:
                    continue
                trip_stops[stop_id].update(
                    {
                        "name": row.get("stop_name", ""),
                        "zone_id": row.get("zone_id", ""),
                    }
                )
        usable = 0
        missing_counts: dict[str, int] = defaultdict(int)
        for v in trip_stops.values():
            missing = missing_stop_fields(
                v.get("trip_id"), v.get("name"), v.get("zone_id")
            )
            if missing:
                for field in missing:
                    missing_counts[field] += 1
            else:
                usable += 1
        return {
            "present": True,
            "location_type_0_rows": location_type_0_rows,
            "usable_rows": usable,
            "missing_field_counts": dict(missing_counts),
        }

    def _print_diagnosis(self, report: dict[str, Any]) -> None:
        print(f"=== Diagnosis: {report['transit_system']} ===")

        fi = report["feed_info"]
        if not fi["present"]:
            print(
                "  feed_info.txt: MISSING - should_update() always reports 'no update needed' without "
                "it, so this system will never auto-ingest on celery-beat's schedule (force=True bypasses this)."
            )
        elif not fi["valid_today"]:
            print(
                f"  feed_info.txt: present, but not valid today (window {fi['feed_start_date']}-{fi['feed_end_date']})"
            )
        else:
            print(
                f"  feed_info.txt: OK (valid {fi['feed_start_date']}-{fi['feed_end_date']})"
            )

        st = report["stop_times"]
        size_note = (
            " - large; watch memory on a small instance"
            if st["row_count"] > 200_000
            else ""
        )
        print(f"  stop_times.txt: {st['row_count']:,} rows{size_note}")

        routes = report["routes"]
        if not routes["present"]:
            print("  routes.txt: MISSING")
        else:
            print(
                f"  routes.txt: {routes['usable_rows']}/{routes['total_rows']} rows usable"
            )
            for field, count in sorted(
                routes["missing_field_counts"].items(), key=lambda kv: -kv[1]
            ):
                print(f"    missing {field}: {count}/{routes['total_rows']} rows")

        trips = report["trips"]
        if not trips["present"]:
            print("  trips.txt: MISSING")
        else:
            by_source = trips["headsign_resolved_by"]
            resolved = (
                ", ".join(f"{k}={v}" for k, v in by_source.items() if k != "unresolved")
                or "none"
            )
            print(
                f"  trips.txt: {trips['total_rows']} rows, headsign resolved via: {resolved}"
            )
            unresolved = by_source.get("unresolved", 0)
            if unresolved:
                print(
                    f"    unresolved (no headsign from any source): {unresolved}/{trips['total_rows']} rows"
                )

        stops = report["stops"]
        if not stops["present"]:
            print("  stops.txt: MISSING")
        else:
            print(
                f"  stops.txt: {stops['usable_rows']}/{stops['location_type_0_rows']} location_type=0 rows usable"
            )
            for field, count in sorted(
                stops["missing_field_counts"].items(), key=lambda kv: -kv[1]
            ):
                print(f"    missing {field}: {count} rows")

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

        # Scan stop_times.txt once, from tmp/ (before promotion) - diagnose()
        # and do_upserts() both need it, and re-scanning it a second time
        # from real_dir() after promotion is exactly the double-parse of the
        # largest GTFS file that exhausted memory on large sources earlier.
        headsign_by_trip, stop_meta, stop_times_rows = self._scan_stop_times(
            self.tmp_file(STOP_TIMES_FILE)
        )
        report = self.diagnose(headsign_by_trip, stop_meta, stop_times_rows)
        if not self._diagnosis_passes(report):
            print(
                f"Diagnosis failed for {self.transit_system} - not promoting or ingesting this fetch. See report above."
            )
            self.remove_tmp()
            return

        self.update_metadata(METADATA_FILE_LIST)
        self.push_updated_metadata_file()
        self.remove_tmp()
        self.do_upserts(headsign_by_trip, stop_meta)

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

    def do_upserts(
        self, headsign_by_trip: dict[str, str], stop_meta: dict[str, dict[str, Any]]
    ) -> None:
        """headsign_by_trip/stop_meta come from fetch_metadata_update's single
        pre-promotion scan of stop_times.txt (see _scan_stop_times) - by the
        time this runs, files are already promoted into real_dir(), so
        re-scanning here would parse the largest GTFS file a second time."""
        with engine.begin() as connection:
            try:
                self.upsert_transit_system(connection)
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
                last_fetched_at=stmt.excluded.last_fetched_at,
            ),
        )
        result = connection.execute(upsert_stmt)
        print(f"upsert transit system result: {result.keys()}")

    def upsert_routes(self, connection: Connection) -> None:
        transit_system_id = self.select_transit_system(connection)
        routes = []
        with open(self.real_file(ROUTES_FILE), "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                route_id = row.get("route_id")
                short_name = row.get("route_short_name")
                long_name = row.get("route_long_name")
                url = resolve_route_url(
                    row.get("route_url"),
                    DEFAULT_SCHEDULE_URL_BY_SYSTEM.get(self.transit_system),
                )
                color = row.get("route_color")
                text_color = row.get("route_text_color")
                if missing_route_fields(
                    route_id, short_name, long_name, url, color, text_color
                ):
                    continue
                routes.append(
                    {
                        "transit_system_id": transit_system_id,
                        "route_id": route_id,
                        "short_name": short_name,
                        "long_name": long_name,
                        "url": url,
                        "color": color,
                        "text_color": text_color,
                    }
                )
        if not routes:
            return
        total = _upsert_batched(
            connection,
            Route,
            routes,
            "uq_short_name",
            ["short_name", "long_name", "url", "color", "text_color"],
        )
        print(f"upsert routes result: {total} rows")

    def _scan_stop_times(
        self, file_path: str | None = None
    ) -> tuple[dict[str, str], dict[str, dict[str, Any]], int]:
        """Single streaming pass over stop_times.txt, producing what both
        upsert_trips and upsert_stops need - previously each parsed this file
        independently, doubling memory/CPU on what is typically the largest
        GTFS file by far. `file_path` defaults to real_file(STOP_TIMES_FILE);
        diagnose() passes a scratch-directory path instead so it never reads
        from (or writes to) the promoted real_dir().

        Returns (headsign_by_trip, stop_meta, row_count):
          headsign_by_trip: trip_id -> earliest-stop_sequence non-empty
            stop_headsign (see schedule_utils.is_earlier_stop_sequence).
          stop_meta: stop_id -> {"trip_id", "stop_headsign"} (last row wins,
            matching the original upsert_stops behavior).
          row_count: total rows read - a proxy for how much memory a fetch
            of this system will use (see README's large-system guidance).
        """
        headsign_by_trip: dict[str, str] = {}
        min_seq_by_trip: dict[str, int] = {}
        stop_meta: dict[str, dict[str, Any]] = {}
        row_count = 0
        with open(
            file_path or self.real_file(STOP_TIMES_FILE), "r", encoding="utf-8-sig"
        ) as f:
            reader = csv.DictReader(f)
            for row in reader:
                row_count += 1
                trip_id = row.get("trip_id")
                headsign = row.get("stop_headsign")
                if trip_id and headsign:
                    seq: int | None
                    try:
                        seq = int(row.get("stop_sequence", "0"))
                    except (TypeError, ValueError):
                        seq = None
                    if seq is not None and is_earlier_stop_sequence(
                        seq, min_seq_by_trip.get(trip_id)
                    ):
                        min_seq_by_trip[trip_id] = seq
                        headsign_by_trip[trip_id] = headsign
                stop_id = row.get("stop_id")
                if stop_id:
                    stop_meta[stop_id] = {
                        "trip_id": trip_id,
                        "stop_headsign": headsign or None,
                    }
        return headsign_by_trip, stop_meta, row_count

    def _build_route_long_names(self, file_path: str | None = None) -> dict[str, str]:
        long_name_by_route: dict[str, str] = {}
        with open(
            file_path or self.real_file(ROUTES_FILE), "r", encoding="utf-8-sig"
        ) as f:
            reader = csv.DictReader(f)
            for row in reader:
                route_id = row.get("route_id")
                long_name = row.get("route_long_name")
                if route_id and long_name:
                    long_name_by_route[route_id] = long_name
        return long_name_by_route

    def upsert_trips(
        self, connection: Connection, stop_headsigns_by_trip: dict[str, str]
    ) -> None:
        rows_to_write = []
        transit_system_id = self.select_transit_system(connection)
        route_long_names = self._build_route_long_names()

        with open(self.real_file(TRIPS_FILE), "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trip_id = row.get("trip_id")
                route_id = row.get("route_id")
                if not trip_id:
                    continue
                name = resolve_trip_headsign(
                    row.get("trip_headsign"),
                    stop_headsigns_by_trip.get(trip_id),
                    route_long_names.get(route_id) if route_id else None,
                )
                direction_id = parse_direction_id(row.get("direction_id"))
                rows_to_write.append(
                    {
                        "trip_id": trip_id,
                        "transit_system_id": transit_system_id,
                        "name": name,
                        "route_id": route_id,
                        "direction_id": direction_id,
                    }
                )
        if not rows_to_write:
            return
        total = _upsert_batched(
            connection,
            Trip,
            rows_to_write,
            "uq_trip",
            ["name", "direction_id"],
        )
        print(f"upsert trips result: {total} rows")

    def upsert_stops(
        self, connection: Connection, stop_meta: dict[str, dict[str, Any]]
    ) -> None:
        transit_system_id = self.select_transit_system(connection)

        trip_stops = defaultdict(dict, stop_meta)
        with open(self.real_file(STOPS_FILE), "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                location_type = row.get("location_type")
                if location_type != "0":
                    continue
                stop_id = row.get("stop_id")

                if not stop_id:
                    continue
                trip_stops[stop_id].update(
                    {
                        "transit_system_id": transit_system_id,
                        "name": row.get("stop_name", ""),
                        "zone_id": row.get("zone_id", ""),
                    }
                )
        if not trip_stops:
            return

        rows_to_write = [
            dict(
                stop_id=k,
                transit_system_id=transit_system_id,
                trip_id=v.get("trip_id"),
                name=v.get("name"),
                zone_id=v.get("zone_id"),
                stop_headsign=v.get("stop_headsign"),
            )
            for k, v in trip_stops.items()
            if not missing_stop_fields(
                v.get("trip_id"), v.get("name"), v.get("zone_id")
            )
        ]

        total = _upsert_batched(
            connection,
            Stop,
            rows_to_write,
            "uq_transit_stop_id",
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
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Download and report on the feed without writing to the database or "
        "promoting it into src/metadata/ - shows feed_info validity, route/stop "
        "completeness with per-field breakdowns, trip headsign resolution by "
        "source, and stop_times.txt size. Not valid with --all.",
    )
    parser.add_argument(
        "--schedule-url",
        help="Use this URL instead of looking --transit-system up in GTFS_METADATA - "
        "lets you diagnose a system before adding it there. Only valid with --diagnose.",
    )
    args = parser.parse_args()

    if args.diagnose:
        if args.all:
            raise SystemExit(
                "--diagnose doesn't support --all - pass --transit-system for one system at a time."
            )
        schedule_url = args.schedule_url or GTFS_METADATA.get(args.transit_system)
        if not schedule_url:
            raise SystemExit(
                f"Invalid transit system: {args.transit_system} (or pass --schedule-url)"
            )
        Fetcher(schedule_url, args.transit_system).diagnose_only()
        raise SystemExit(0)

    if args.schedule_url:
        raise SystemExit("--schedule-url is only valid with --diagnose.")

    systems: list[tuple[str, str | None]] = (
        list(GTFS_METADATA.items())
        if args.all
        else [(args.transit_system, GTFS_METADATA.get(args.transit_system))]
    )
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
