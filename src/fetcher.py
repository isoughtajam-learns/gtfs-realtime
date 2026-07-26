"""
Fetcher is meant to check for updates to GTFS Schedule data for a transit system, parse the files, and store them for use.

Current scope:
trips.txt for trip_id and headsign
stops.txt for stop_id and stop_name
"""
import csv
import io
import os
import pathlib
import shutil
import zipfile
from datetime import datetime, date
from pathlib import Path

import requests


TMP_DIR = "src/tmp/"
METADATA_DIR = "src/metadata/"
METADATA_FILE = "feed_info.txt"
TRIPS_FILE = "trips.txt"
STOPS_FILE = "stops.txt"
UPDATED_METADATA_FILE = "updated_metadata.txt"

class Fetcher:
    url: str
    transit_system: str

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
        with open(f"{self.tmp_dir()}{METADATA_FILE}", mode= "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                start = datetime.strptime(row.get("feed_start_date", "19700101"), "%Y%m%d")
                end = datetime.strptime(row.get("feed_end_date", "19700101"), "%Y%m%d")
                if not files_exist or not start <= datetime.today() <= end:
                    return True
        return False

    def update_metadata(self, file_name: str) -> None:
        if not os.path.isdir(self.real_dir()):
            os.makedirs(self.real_dir())
        tmp_file = Path(f"{self.tmp_file(file_name)}")
        real_file = Path(f"{self.real_file(file_name)}")
        tmp_file.rename(real_file)

    def push_updated_metadata_file(self) -> None:
        with open(self.real_file(UPDATED_METADATA_FILE), "w") as f:
            f.write(datetime.today().strftime("%Y%m%d"))

    def remove_tmp(self) -> None:
        if os.path.isdir(self.tmp_dir()):
            shutil.rmtree(Path(self.tmp_dir()))

    def fetch_metadata_update(self) -> None:
        response = requests.get(self.url)
        # Extract contents directly in memory
        if response.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                z.extractall(f"{TMP_DIR}{self.transit_system}")
        if not self.should_update():
            print(f"No updates needed from GTFS Schedule data - {self.transit_system}")
            return

        self.update_metadata(TRIPS_FILE)
        self.update_metadata(STOPS_FILE)
        self.push_updated_metadata_file()
        self.remove_tmp()


if __name__ == "__main__":
    # Example usage with BART
    fetcher = Fetcher("http://www.bart.gov/dev/schedules/google_transit.zip", "BART")
    fetcher.fetch_metadata_update()
