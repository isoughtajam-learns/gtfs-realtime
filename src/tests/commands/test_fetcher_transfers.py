# src/tests/commands/test_fetcher_transfers.py
from typing import Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.commands import fetcher as fetcher_module
from src.commands.fetcher import Fetcher
from src.models import Transfer


def test_upsert_transfers_inserts_valid_and_skips_invalid(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    """Transfers are inserted correctly and invalid ones are skipped."""
    fetcher = make_fetcher(
        fetcher_module.TRANSFERS_FILE,
        "transfer_id,route_id,from_stop_code,from_stop_sequence,transit_time,destination_route_id,to_stop_code,to_stop_sequence\n"
        "T1,R1,S1,1,5,R2,S2,2\n"
        "T2,,S3,3,10,,S4,4\n"
        "T3,R3,S5,5,invalid,R4,S6,6\n",
    )

    with db_engine.begin() as conn:
        fetcher.upsert_transfers(conn)
        rows = conn.execute(select(Transfer)).fetchall()

    # Only the two valid rows should be present
    assert len(rows) == 2

    row1 = next(r for r in rows if r.transfer_id == "T1")
    assert row1.route_id == "R1"
    assert row1.from_stop_code == "S1"
    assert row1.from_stop_sequence == 1
    assert row1.transit_time == 5
    assert row1.destination_route_id == "R2"
    assert row1.to_stop_code == "S2"
    assert row1.to_stop_sequence == 2

    # Second row has optional fields omitted
    row2 = next(r for r in rows if r.transfer_id == "T2")
    assert row2.route_id is None
    assert row2.destination_route_id is None
    assert row2.from_stop_code == "S3"
    assert row2.from_stop_sequence == 3
    assert row2.transit_time == 10
    assert row2.to_stop_code == "S4"
    assert row2.to_stop_sequence == 4

    # The invalid row (T3, non-numeric transit_time) should not be present
    assert all(r.transfer_id != "T3" for r in rows)


def test_upsert_transfers_dedupe_duplicates(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    """Duplicate transfer_ids are deduplicated with last write wins."""
    fetcher = make_fetcher(
        fetcher_module.TRANSFERS_FILE,
        "transfer_id,route_id,from_stop_code,from_stop_sequence,transit_time,destination_route_id,to_stop_code,to_stop_sequence\n"
        "T1,R1,S1,1,5,R2,S2,2\n"
        "T2,,S3,3,10,,S4,4\n"
        "T1,R1_dup,S1,1,6,R2_dup,S2,2\n",  # duplicate with updated values
    )

    with db_engine.begin() as conn:
        fetcher.upsert_transfers(conn)
        rows = conn.execute(select(Transfer)).fetchall()

    assert len(rows) == 2
    assert {r.transfer_id for r in rows} == {"T1", "T2"}

    # T1 reflects the data from the second (duplicate) row
    t1_row = next(r for r in rows if r.transfer_id == "T1")
    assert t1_row.route_id == "R1_dup"
    assert t1_row.transit_time == 6
    assert t1_row.destination_route_id == "R2_dup"

    # T2 remains unchanged
    t2_row = next(r for r in rows if r.transfer_id == "T2")
    assert t2_row.route_id is None
    assert t2_row.transit_time == 10
