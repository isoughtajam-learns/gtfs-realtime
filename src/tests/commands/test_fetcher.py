from pathlib import Path

from src.commands.fetcher import Fetcher


def _write_stop_times_with_bom(path: Path) -> None:
    # Mirrors a real UTF-8-with-BOM GTFS export: the file's raw bytes start
    # with EF BB BF (the UTF-8 encoding of U+FEFF) stuck directly in front of
    # the first header. _scan_stop_times must decode with "utf-8-sig" so that
    # BOM never becomes part of the "trip_id" key.
    content = (
        "trip_id,stop_id,stop_sequence,stop_headsign\nT1,S1,1,Downtown\nT1,S2,2,\n"
    )
    path.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))


def test_scan_stop_times_strips_bom_from_first_column_so_trip_id_is_found(
    tmp_path: Path,
) -> None:
    # Without decoding as utf-8-sig, row.get("trip_id") would return None
    # for every row (the real key is "﻿trip_id"), silently dropping
    # every trip's headsign and stop metadata rather than raising - this
    # test guards against that regression.
    stop_times = tmp_path / "stop_times.txt"
    _write_stop_times_with_bom(stop_times)

    fetcher = Fetcher(url="http://example.com/gtfs.zip", transit_system="Test_System")
    headsign_by_trip, stop_meta, row_count = fetcher._scan_stop_times(str(stop_times))

    assert row_count == 2
    assert headsign_by_trip == {"T1": "Downtown"}
    assert stop_meta["S1"]["trip_id"] == "T1"
    assert stop_meta["S2"]["trip_id"] == "T1"
