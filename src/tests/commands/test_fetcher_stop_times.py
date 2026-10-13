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
