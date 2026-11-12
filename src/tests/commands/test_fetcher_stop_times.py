# src/tests/commands/test_fetcher_stop_times.py
from typing import Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.commands import fetcher as fetcher_module
from src.commands.fetcher import Fetcher
from src.models import StopTime


def test_upsert_stop_times_inserts_and_skips_invalid(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(
        fetcher_module.STOP_TIMES_FILE,
        "trip_id,stop_id,stop_sequence\nT1,S1,1\nT1,S2,2\nT1,S3,invalid",
    )

    with db_engine.begin() as conn:
        fetcher.upsert_stop_times(conn)
        rows = conn.execute(select(StopTime)).fetchall()

    assert len(rows) == 2
    seqs = {r.stop_sequence for r in rows}
    assert 1 in seqs and 2 in seqs


def test_upsert_stop_times_dedupe_conflict(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(
        fetcher_module.STOP_TIMES_FILE,
        "trip_id,stop_id,stop_sequence\nT1,S1,1\nT1,S1,1",  # duplicate row
    )

    with db_engine.begin() as conn:
        fetcher.upsert_stop_times(conn)
        rows = conn.execute(select(StopTime)).fetchall()

    # dedupe prevents duplicate rows – only one should exist
    assert len(rows) == 1


def test_upsert_stop_times_parses_arrival_and_departure_seconds(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(
        fetcher_module.STOP_TIMES_FILE,
        "trip_id,stop_id,stop_sequence,arrival_time,departure_time\n"
        "T1,S1,1,06:00:00,06:00:30\n"
        # Past-midnight trip - must not wrap to 01:30:00.
        "T1,S2,2,25:30:00,25:31:00\n"
        "T1,S3,3,,\n",  # blank times - legal for interpolated stops
    )

    with db_engine.begin() as conn:
        fetcher.upsert_stop_times(conn)
        rows = {r.stop_sequence: r for r in conn.execute(select(StopTime)).fetchall()}

    assert rows[1].arrival_time_seconds == 6 * 3600
    assert rows[1].departure_time_seconds == 6 * 3600 + 30
    assert rows[2].arrival_time_seconds == 25 * 3600 + 30 * 60
    assert rows[3].arrival_time_seconds is None
    assert rows[3].departure_time_seconds is None


def test_upsert_stop_times_updates_times_on_conflict(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(
        fetcher_module.STOP_TIMES_FILE,
        "trip_id,stop_id,stop_sequence,arrival_time,departure_time\n"
        "T1,S1,1,06:00:00,06:00:30\n",
    )
    with db_engine.begin() as conn:
        fetcher.upsert_stop_times(conn)

    fetcher2 = make_fetcher(
        fetcher_module.STOP_TIMES_FILE,
        "trip_id,stop_id,stop_sequence,arrival_time,departure_time\n"
        "T1,S1,1,07:15:00,07:15:30\n",
    )
    with db_engine.begin() as conn:
        fetcher2.upsert_stop_times(conn)
        row = conn.execute(select(StopTime)).fetchone()

    assert row is not None
    assert row.arrival_time_seconds == 7 * 3600 + 15 * 60
    assert row.departure_time_seconds == 7 * 3600 + 15 * 60 + 30
