from pathlib import Path

import pytest

from src.services import trip_detail


def _write_stop_times(path: Path, rows: str) -> None:
    path.write_text("trip_id,stop_id,stop_sequence\n" + rows)


def test_get_scheduled_tail_stops_returns_stops_after_last_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit_system_dir = tmp_path / "BART"
    transit_system_dir.mkdir()
    _write_stop_times(
        transit_system_dir / "stop_times.txt",
        "T1,S1,1\nT1,S2,2\nT1,S3,3\nT1,S4,4\n",
    )
    monkeypatch.setattr(trip_detail, "METADATA_DIR", f"{tmp_path}/")

    result = trip_detail.get_scheduled_tail_stops("BART", "T1", ["S1", "S2"])

    assert result == [(3, "S3"), (4, "S4")]


def test_get_scheduled_tail_stops_empty_when_last_covered_is_final_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit_system_dir = tmp_path / "BART"
    transit_system_dir.mkdir()
    _write_stop_times(transit_system_dir / "stop_times.txt", "T1,S1,1\nT1,S2,2\n")
    monkeypatch.setattr(trip_detail, "METADATA_DIR", f"{tmp_path}/")

    assert trip_detail.get_scheduled_tail_stops("BART", "T1", ["S1", "S2"]) == []


def test_get_scheduled_tail_stops_empty_when_last_covered_id_not_in_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # e.g. MBTA's realtime/schedule ID mismatch - safer to show nothing
    # extra than guess where an unrecognized stop_id fits in the sequence.
    transit_system_dir = tmp_path / "MBTA"
    transit_system_dir.mkdir()
    _write_stop_times(transit_system_dir / "stop_times.txt", "T1,S1,1\nT1,S2,2\n")
    monkeypatch.setattr(trip_detail, "METADATA_DIR", f"{tmp_path}/")

    assert (
        trip_detail.get_scheduled_tail_stops("MBTA", "T1", ["not-a-real-stop-id"]) == []
    )


def test_get_scheduled_tail_stops_empty_when_trip_not_in_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit_system_dir = tmp_path / "BART"
    transit_system_dir.mkdir()
    _write_stop_times(transit_system_dir / "stop_times.txt", "T2,S1,1\nT2,S2,2\n")
    monkeypatch.setattr(trip_detail, "METADATA_DIR", f"{tmp_path}/")

    assert trip_detail.get_scheduled_tail_stops("BART", "T1", ["S1"]) == []


def test_get_scheduled_tail_stops_empty_when_stop_times_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trip_detail, "METADATA_DIR", f"{tmp_path}/")

    assert trip_detail.get_scheduled_tail_stops("BART", "T1", ["S1"]) == []


def test_get_scheduled_tail_stops_empty_when_live_stop_ids_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit_system_dir = tmp_path / "BART"
    transit_system_dir.mkdir()
    _write_stop_times(transit_system_dir / "stop_times.txt", "T1,S1,1\nT1,S2,2\n")
    monkeypatch.setattr(trip_detail, "METADATA_DIR", f"{tmp_path}/")

    assert trip_detail.get_scheduled_tail_stops("BART", "T1", []) == []


def test_get_scheduled_tail_stops_ignores_other_trips_and_sorts_by_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rows for T1 are deliberately out of file order and interleaved with a
    # different trip's rows - the function must filter to T1 and sort by
    # stop_sequence rather than trusting file order.
    transit_system_dir = tmp_path / "BART"
    transit_system_dir.mkdir()
    _write_stop_times(
        transit_system_dir / "stop_times.txt",
        "T1,S3,3\nT2,S9,1\nT1,S1,1\nT1,S2,2\n",
    )
    monkeypatch.setattr(trip_detail, "METADATA_DIR", f"{tmp_path}/")

    assert trip_detail.get_scheduled_tail_stops("BART", "T1", ["S1"]) == [
        (2, "S2"),
        (3, "S3"),
    ]


def test_get_scheduled_tail_stops_skips_duplicate_stop_id_at_turnback_station(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real BART data: SFO (Y10-1) on the Yellow line is a turnback station -
    # the train dwells and reverses, so the static schedule legitimately
    # lists the same stop_id twice in a row before continuing to Millbrae
    # (W40-3). The live feed has already reported both SFO events - the
    # backfill must jump straight to Millbrae, not re-surface SFO as a
    # bogus duplicate of a stop already shown live.
    transit_system_dir = tmp_path / "BART"
    transit_system_dir.mkdir()
    _write_stop_times(
        transit_system_dir / "stop_times.txt",
        "T1,W30-1,25\nT1,Y10-1,26\nT1,Y10-1,27\nT1,W40-3,28\n",
    )
    monkeypatch.setattr(trip_detail, "METADATA_DIR", f"{tmp_path}/")

    result = trip_detail.get_scheduled_tail_stops(
        "BART", "T1", ["W30-1", "Y10-1", "Y10-1"]
    )

    assert result == [(28, "W40-3")]


def test_get_scheduled_tail_stops_still_pending_second_occurrence_at_turnback_station(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same turnback station, but the live feed has only reported SFO once
    # so far (the train is still dwelling, hasn't reversed yet) - the second
    # SFO event is a real, not-yet-live stop and must still be backfilled.
    transit_system_dir = tmp_path / "BART"
    transit_system_dir.mkdir()
    _write_stop_times(
        transit_system_dir / "stop_times.txt",
        "T1,W30-1,25\nT1,Y10-1,26\nT1,Y10-1,27\nT1,W40-3,28\n",
    )
    monkeypatch.setattr(trip_detail, "METADATA_DIR", f"{tmp_path}/")

    result = trip_detail.get_scheduled_tail_stops("BART", "T1", ["W30-1", "Y10-1"])

    assert result == [(27, "Y10-1"), (28, "W40-3")]


def test_get_scheduled_tail_stops_handles_distant_loop_revisit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stop_id revisited much later (not adjacently, e.g. a loop shuttle)
    # must not be confused with a turnback dwell - anchoring on the Nth
    # occurrence (not just "the last one anywhere in the file") keeps the
    # real intermediate loop stops in the backfill instead of skipping them.
    transit_system_dir = tmp_path / "BART"
    transit_system_dir.mkdir()
    _write_stop_times(
        transit_system_dir / "stop_times.txt",
        "T1,A,1\nT1,B,2\nT1,C,3\nT1,A,4\nT1,D,5\n",
    )
    monkeypatch.setattr(trip_detail, "METADATA_DIR", f"{tmp_path}/")

    result = trip_detail.get_scheduled_tail_stops("BART", "T1", ["A"])

    assert result == [(2, "B"), (3, "C"), (4, "A"), (5, "D")]
