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
    """Transfers are inserted correctly and rows missing a required stop are skipped."""
    fetcher = make_fetcher(
        fetcher_module.TRANSFERS_FILE,
        "from_stop_id,to_stop_id,from_route_id,to_route_id,transfer_type,min_transfer_time\n"
        "S1,S2,R1,R2,2,300\n"
        "S3,S4,,,0,\n"
        ",S6,R3,R4,2,120\n",
    )

    with db_engine.begin() as conn:
        fetcher.upsert_transfers(conn)
        rows = conn.execute(select(Transfer)).fetchall()

    # Only the two rows with both stops present should be written
    assert len(rows) == 2

    row1 = next(r for r in rows if r.from_stop_id == "S1")
    assert row1.to_stop_id == "S2"
    assert row1.from_route_id == "R1"
    assert row1.to_route_id == "R2"
    assert row1.transfer_type == 2
    assert row1.min_transfer_time == 300

    # Second row has optional fields omitted
    row2 = next(r for r in rows if r.from_stop_id == "S3")
    assert row2.to_stop_id == "S4"
    assert row2.from_route_id is None
    assert row2.to_route_id is None
    assert row2.transfer_type == 0
    assert row2.min_transfer_time is None

    # The invalid row (missing from_stop_id) should not be present
    assert all(r.to_stop_id != "S6" for r in rows)


def test_upsert_transfers_dedupe_duplicates(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    """Duplicate (from_stop_id, to_stop_id) pairs are deduplicated, last write wins."""
    fetcher = make_fetcher(
        fetcher_module.TRANSFERS_FILE,
        "from_stop_id,to_stop_id,from_route_id,to_route_id,transfer_type,min_transfer_time\n"
        "S1,S2,R1,R2,2,300\n"
        "S3,S4,,,0,\n"
        "S1,S2,R1_dup,R2_dup,1,\n",  # duplicate stop pair with updated values
    )

    with db_engine.begin() as conn:
        fetcher.upsert_transfers(conn)
        rows = conn.execute(select(Transfer)).fetchall()

    assert len(rows) == 2
    assert {(r.from_stop_id, r.to_stop_id) for r in rows} == {
        ("S1", "S2"),
        ("S3", "S4"),
    }

    # (S1, S2) reflects the data from the second (duplicate) row
    row_s1 = next(r for r in rows if r.from_stop_id == "S1")
    assert row_s1.from_route_id == "R1_dup"
    assert row_s1.to_route_id == "R2_dup"
    assert row_s1.transfer_type == 1
    assert row_s1.min_transfer_time is None

    # (S3, S4) remains unchanged
    row_s3 = next(r for r in rows if r.from_stop_id == "S3")
    assert row_s3.from_route_id is None
    assert row_s3.transfer_type == 0
