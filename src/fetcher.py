"""
Fetcher is meant to check for updates to GTFS Schedule data for a transit system, parse the files, and store them for use.

Current scope:
trips.txt for trip_id and headsign
stops.txt for stop_id and stop_name
"""
import csv
import io
import os
import shutil
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import List

import requests
from sqlalchemy import select, Connection
from sqlalchemy.dialects.postgresql import insert

from database import engine
from constants import GTFS_URLS, GTFS_METADATA, DEFAULT_SCHEDULE_URL_BY_SYSTEM
from models import TransitSystem, Trip, Stop, Route


TMP_DIR = "src/tmp/"
METADATA_DIR = "src/metadata/"
METADATA_FILE = "feed_info.txt"
TRIPS_FILE = "trips.txt"
STOPS_FILE = "stops.txt"
STOP_TIMES_FILE = "stop_times.txt"
ROUTES_FILE = "routes.txt"
METADATA_FILE_LIST = [TRIPS_FILE, STOPS_FILE, STOP_TIMES_FILE, ROUTES_FILE]
UPDATED_METADATA_FILE = "updated_metadata.txt"


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

    def remove_tmp(self) -> None:
        if os.path.isdir(self.tmp_dir()):
            shutil.rmtree(Path(self.tmp_dir()))

    def fetch_metadata_update(self) -> None:
        if self.updated_recently():
            return

        response = requests.get(self.url)
        if response.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                z.extractall(f"{TMP_DIR}{self.transit_system}")

        if not self.should_update():
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
                self.upsert_trips(connection)
                self.upsert_stops(connection)
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
        )
        upsert_stmt = stmt.on_conflict_do_update(
            constraint="uq_name",
            set_=dict(
                realtime_url=stmt.excluded.realtime_url,
                schedule_url=stmt.excluded.schedule_url),
        )
        connection.execute(upsert_stmt)

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
        insert_stmt = insert(Route).values(routes)
        upsert_stmt = insert_stmt.on_conflict_do_update(
            constraint="uq_short_name",
            set_=dict(
                short_name=insert_stmt.excluded.short_name,
                long_name=insert_stmt.excluded.long_name,
                url=insert_stmt.excluded.url,
                color=insert_stmt.excluded.color,
                text_color=insert_stmt.excluded.text_color,
            ),
        )
        connection.execute(upsert_stmt)

    def upsert_trips(self, connection: Connection) -> None:
        rows_to_write = []
        transit_system_id = self.select_transit_system(connection)

        with open(self.real_file(TRIPS_FILE), "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trip_id = row.get("trip_id")
                name = row.get("trip_headsign")
                route_id = row.get("route_id")
                if not trip_id or not name:
                    continue
                rows_to_write.append({
                    "trip_id": int(trip_id),
                    "transit_system_id": transit_system_id,
                    "name": name,
                    "route_id": int(route_id),
                })
        if not rows_to_write:
            return
        insert_stmt = insert(Trip).values(rows_to_write)
        upsert_stmt = insert_stmt.on_conflict_do_update(
            constraint="uq_trip",
            set_=dict(
                name=insert_stmt.excluded.name,
            ),
        )
        connection.execute(upsert_stmt)

    def upsert_stops(self, connection: Connection) -> None:
        transit_system_id = self.select_transit_system(connection)

        trip_stops = defaultdict(dict)
        with open(self.real_file(STOP_TIMES_FILE), "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trip_stops[row.get('stop_id')] = {
                    "trip_id": row.get('trip_id'),
                }
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
            zone_id=v.get("zone_id")
        ) for k, v in trip_stops.items() if all([
            v.get("trip_id"),
            v.get("name"),
            v.get("zone_id")
        ])]

        insert_stmt = insert(Stop).values(rows_to_write)
        upsert_stmt = insert_stmt.on_conflict_do_update(
            constraint="uq_transit_stop_id",
            set_=dict(
                name=insert_stmt.excluded.name,
            ),
        )
        connection.execute(upsert_stmt)


if __name__ == "__main__":
    # Example usage with BART
    fetcher = Fetcher("http://www.bart.gov/dev/schedules/google_transit.zip", "BART")
    fetcher.fetch_metadata_update()
