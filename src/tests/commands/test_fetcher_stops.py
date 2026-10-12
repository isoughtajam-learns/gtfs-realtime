# src/tests/commands/test_fetcher_stops.py
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.commands import fetcher as fetcher_module
from src.commands.fetcher import Fetcher
from src.models import Stop


def test_upsert_stops_inserts_and_skips_missing_fields(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(
        fetcher_module.STOPS_FILE,
        "stop_id,stop_name,location_type\nS1,Downtown,0\nS2,,0\nS3,Uptown,0",
    )

    # upsert_stops requires a trip_id (sourced from stop_times.txt) for a
    # stop to be usable - see missing_stop_fields() - so stop_meta must
    # supply one for each stop_id for this test's rows to pass that check.
    stop_meta: dict[str, dict[str, Any]] = {
        "S1": {"trip_id": "T1", "stop_headsign": None},
        "S2": {"trip_id": "T1", "stop_headsign": None},
        "S3": {"trip_id": "T1", "stop_headsign": None},
    }

    with db_engine.begin() as conn:
        fetcher.upsert_stops(conn, stop_meta)
        rows = conn.execute(select(Stop)).fetchall()

    # S2 has no stop_name - missing_stop_fields() requires one, so it's
    # skipped entirely rather than stored with a None name.
    assert len(rows) == 2
    assert {r.stop_id for r in rows} == {"S1", "S3"}


def test_upsert_stops_dedupe_by_stop_id(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(
        fetcher_module.STOPS_FILE,
        "stop_id,stop_name,location_type\nS1,A,0\nS1,B,0",  # duplicate stop_id
    )
    stop_meta: dict[str, dict[str, Any]] = {
        "S1": {"trip_id": "T1", "stop_headsign": None}
    }

    with db_engine.begin() as conn:
        fetcher.upsert_stops(conn, stop_meta)
        rows = conn.execute(select(Stop)).fetchall()

    # last write wins – stop_name should be "B"
    assert len(rows) == 1
    assert rows[0].stop_name == "B"
