"""
One-shot, read-only comparison between a transit_system's own direct
511.org GTFS Schedule download and the combined `operator_id=RG` regional
download - the live verification IRL-18 needs before deciding whether the
"one fetch covers everyone" simplification already confirmed for the
realtime side (see src/services/regional_feed.py, IRL-14 through IRL-17)
also applies to the static Schedule side. Makes two real downloads
(direct + RG) and prints a structural report: file lists, row counts, and
a same-route ID-format comparison. Touches no database, writes nothing
into src/metadata/ or src/tmp/ - entirely separate temp directories,
cleaned up automatically.

Usage:
    API_KEY_511_ORG=... python -m src.commands.regional_schedule_diagnose \\
        --transit-system SF-MTA --agency-code SF
"""

import argparse
import csv
import io
import logging
import tempfile
import zipfile
from pathlib import Path
from typing import List

import requests

from src.settings import get_settings

logger = logging.getLogger(__name__)

DATAFEEDS_URL = "http://api.511.org/transit/datafeeds"

# The GTFS files this app's own Fetcher actually ingests (see
# src/commands/fetcher.py's METADATA_FILE_LIST) - the ones worth a
# row-count/ID-format comparison. agency.txt and feed_info.txt are
# included for the regional feed's own metadata, not because Fetcher reads
# every field of them the same way.
FILES_OF_INTEREST = [
    "agency.txt",
    "routes.txt",
    "stops.txt",
    "trips.txt",
    "stop_times.txt",
    "transfers.txt",
]


def _download_schedule_zip(operator_id: str, api_key: str) -> bytes:
    response = requests.get(
        DATAFEEDS_URL,
        params={"api_key": api_key, "operator_id": operator_id},
        timeout=120,
    )
    response.raise_for_status()
    return response.content


def _extract(zip_bytes: bytes, dest: Path) -> List[str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = z.namelist()
        z.extractall(dest)
    return names


def _row_count(path: Path) -> int:
    if not path.is_file():
        return -1
    with open(path, "r", encoding="utf-8-sig") as f:
        return sum(1 for _ in csv.reader(f)) - 1  # minus header


def _first_rows(path: Path, column: str, n: int = 5) -> List[str]:
    if not path.is_file():
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        values = []
        for row in reader:
            value = row.get(column)
            if value:
                values.append(value)
            if len(values) >= n:
                break
        return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transit-system",
        required=True,
        help="This app's TransitSystem.name, for display only, e.g. SF-MTA.",
    )
    parser.add_argument(
        "--agency-code",
        required=True,
        help="511.org agency/operator code for the same system, e.g. SF.",
    )
    args = parser.parse_args()

    api_key = get_settings().api_key_511_org
    if not api_key:
        raise SystemExit(
            "API_KEY_511_ORG is not set - export it before running this script."
        )

    print(f"Downloading direct ({args.agency_code}) schedule zip...")
    direct_bytes = _download_schedule_zip(args.agency_code, api_key)
    print("Downloading RG (regional) schedule zip...")
    rg_bytes = _download_schedule_zip("RG", api_key)
    print(
        f"direct zip: {len(direct_bytes):,} bytes, RG zip: {len(rg_bytes):,} bytes "
        f"({len(rg_bytes) / max(len(direct_bytes), 1):.1f}x)"
    )
    print()

    with (
        tempfile.TemporaryDirectory() as direct_dir,
        tempfile.TemporaryDirectory() as rg_dir,
    ):
        direct_names = _extract(direct_bytes, Path(direct_dir))
        rg_names = _extract(rg_bytes, Path(rg_dir))

        print("=== Zip structure ===")
        print(f"direct ({args.agency_code}) top-level entries: {sorted(direct_names)}")
        print(f"RG top-level entries: {sorted(rg_names)}")
        # A flat GTFS zip has files directly at the root (no "/" in any
        # name); a per-agency-subfoldered zip would show up here as
        # entries like "SF/routes.txt".
        rg_has_subfolders = any("/" in name for name in rg_names)
        print(f"RG zip has subfolders: {rg_has_subfolders}")
        print()

        print("=== Row counts (direct vs RG) ===")
        for file_name in FILES_OF_INTEREST:
            direct_count = _row_count(Path(direct_dir) / file_name)
            rg_count = _row_count(Path(rg_dir) / file_name)
            print(f"{file_name:20s} direct={direct_count:>10} RG={rg_count:>10}")
        print()

        print("=== Sample route_id values (first 5) ===")
        direct_route_ids = _first_rows(Path(direct_dir) / "routes.txt", "route_id")
        rg_route_ids = _first_rows(Path(rg_dir) / "routes.txt", "route_id")
        print(f"direct ({args.agency_code}): {direct_route_ids}")
        print(f"RG: {rg_route_ids}")
        print()

        print("=== Sample stop_id values (first 5) ===")
        direct_stop_ids = _first_rows(Path(direct_dir) / "stops.txt", "stop_id")
        rg_stop_ids = _first_rows(Path(rg_dir) / "stops.txt", "stop_id")
        print(f"direct ({args.agency_code}): {direct_stop_ids}")
        print(f"RG: {rg_stop_ids}")
        print()

        # If RG's routes.txt combines every agency, a route_id from the
        # direct (single-agency) download should also be findable
        # somewhere in RG's routes.txt - either verbatim or under some
        # prefixed/transformed form. This is the key signal for whether
        # "one fetch covers everyone" holds for Schedule the way it does
        # for the realtime side.
        if direct_route_ids:
            sample = direct_route_ids[0]
            rg_routes_path = Path(rg_dir) / "routes.txt"
            found_verbatim = sample in _column_values(rg_routes_path, "route_id")
            found_as_substring = any(
                sample in v for v in _column_values(rg_routes_path, "route_id")
            )
            print(
                "=== Cross-check: does RG's routes.txt contain the direct feed's route_id? ==="
            )
            print(f"Sample direct route_id: {sample!r}")
            print(f"Found verbatim in RG routes.txt: {found_verbatim}")
            print(f"Found as a substring in some RG route_id: {found_as_substring}")


def _column_values(path: Path, column: str) -> List[str]:
    if not path.is_file():
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return [row[column] for row in reader if row.get(column)]


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    main()
