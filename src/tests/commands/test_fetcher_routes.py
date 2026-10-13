# src/tests/commands/test_fetcher_routes.py
from typing import Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.commands import fetcher as fetcher_module
from src.commands.fetcher import Fetcher
from src.models import Route


def test_upsert_routes_inserts_and_skips_invalid(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(
        fetcher_module.ROUTES_FILE,
        "route_id,route_short_name,route_long_name,route_color,route_text_color,route_type\nR1,1,Line 1,FF0000,FFFFFF,3\nR2,2,,FF00FF,000000,3",
    )

    with db_engine.begin() as conn:
        fetcher.upsert_routes(conn)
        rows = conn.execute(select(Route)).fetchall()

    # R2 has an empty long_name – missing_route_fields() requires one, so
    # it's skipped entirely rather than stored with a blank long_name.
    assert len(rows) == 1
    assert rows[0].route_id == "R1"
    assert rows[0].long_name == "Line 1"


def test_upsert_routes_dedupe_by_short_name(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(
        fetcher_module.ROUTES_FILE,
        "route_id,route_short_name,route_long_name,route_color,route_text_color,route_type\nR1,1,First,FF0000,FFFFFF,3\nR2,1,Second,FF00FF,000000,3",
    )

    with db_engine.begin() as conn:
        fetcher.upsert_routes(conn)
        rows = conn.execute(select(Route)).fetchall()

    # unique constraint on short_name – second row should overwrite
    assert len(rows) == 1
    assert rows[0].long_name == "Second"
