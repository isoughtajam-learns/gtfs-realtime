# src/tests/commands/test_fetcher_trips.py
#
# No prior test exercised upsert_trips directly (test_fetcher.py only covers
# its _scan_stop_times/_build_stop_names helpers) - unlike make_fetcher
# (src/tests/common.py), upsert_trips also reads ROUTES_FILE/STOPS_FILE via
# _build_route_long_names/_build_stop_names, so this uses its own fixture
# that stubs real_file for all three names instead of just one.
from pathlib import Path
from typing import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.commands import fetcher as fetcher_module
from src.commands.fetcher import Fetcher
from src.models import Trip


@pytest.fixture  # type: ignore[misc]
def make_trips_fetcher(
    tmp_path: Path,
    transit_system_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str], Fetcher]:
    def _make(trips_content: str) -> Fetcher:
        trips_path = tmp_path / fetcher_module.TRIPS_FILE
        trips_path.write_text(trips_content)
        routes_path = tmp_path / fetcher_module.ROUTES_FILE
        routes_path.write_text("route_id,route_long_name\n")
        stops_path = tmp_path / fetcher_module.STOPS_FILE
        stops_path.write_text("stop_id,stop_name\n")

        fetcher = Fetcher(url="", transit_system="gtfs_test_system")
        fetcher.transit_system_id = transit_system_id
        paths = {
            fetcher_module.TRIPS_FILE: str(trips_path),
            fetcher_module.ROUTES_FILE: str(routes_path),
            fetcher_module.STOPS_FILE: str(stops_path),
        }
        monkeypatch.setattr(fetcher, "real_file", lambda name: paths.get(name, ""))
        monkeypatch.setattr(
            fetcher, "_default_schedule_url", lambda: "http://example.com/routes"
        )
        return fetcher

    return _make


def test_upsert_trips_captures_service_id(
    db_engine: Engine, make_trips_fetcher: Callable[[str], Fetcher]
) -> None:
    fetcher = make_trips_fetcher(
        "trip_id,route_id,trip_headsign,direction_id,service_id\n"
        "T1,R1,Downtown,0,WEEKDAY\n"
        "T2,R1,Downtown,1,\n"  # blank service_id - legal, stays None
    )

    with db_engine.begin() as conn:
        fetcher.upsert_trips(conn, {}, {})
        rows = {r.trip_id: r for r in conn.execute(select(Trip)).fetchall()}

    assert rows["T1"].service_id == "WEEKDAY"
    assert rows["T2"].service_id is None


def test_upsert_trips_updates_service_id_on_conflict(
    db_engine: Engine, make_trips_fetcher: Callable[[str], Fetcher]
) -> None:
    fetcher = make_trips_fetcher(
        "trip_id,route_id,trip_headsign,direction_id,service_id\n"
        "T1,R1,Downtown,0,WEEKDAY\n"
    )
    with db_engine.begin() as conn:
        fetcher.upsert_trips(conn, {}, {})

    fetcher2 = make_trips_fetcher(
        "trip_id,route_id,trip_headsign,direction_id,service_id\n"
        "T1,R1,Downtown,0,WEEKEND\n"
    )
    with db_engine.begin() as conn:
        fetcher2.upsert_trips(conn, {}, {})
        row = conn.execute(select(Trip).where(Trip.trip_id == "T1")).fetchone()

    assert row is not None
    assert row.service_id == "WEEKEND"
