# src/tests/commands/test_fetcher_routes.py
from typing import Callable

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from src.commands import fetcher as fetcher_module
from src.commands.fetcher import Fetcher
from src.models import Route, TransitSystem


def test_upsert_routes_inserts_and_skips_invalid(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(
        fetcher_module.ROUTES_FILE,
        "route_id,route_short_name,route_long_name,route_color,route_text_color,route_type\n"
        "R1,1,Line 1,FF0000,FFFFFF,3\n"
        "R2,2,,FF00FF,000000,3\n"
        "R3,,Line 3,FF00FF,000000,3",
    )

    with db_engine.begin() as conn:
        fetcher.upsert_routes(conn)
        rows = conn.execute(select(Route)).fetchall()

    # R2 has an empty long_name - long_name is optional per the GTFS spec
    # (only short_name/long_name need one of the pair, and short_name is
    # present here), so it's still stored, just with long_name=None rather
    # than a blank string. R3 has an empty short_name - that one IS required
    # (it's part of Route's primary key), so R3 is skipped entirely.
    by_route_id = {r.route_id: r for r in rows}
    assert set(by_route_id) == {"R1", "R2"}
    assert by_route_id["R1"].long_name == "Line 1"
    assert by_route_id["R2"].long_name is None


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


def test_upsert_routes_same_short_name_in_different_systems_creates_separate_rows(
    db_engine: Engine,
    transit_system_id: int,
    make_fetcher: Callable[[str, str], Fetcher],
) -> None:
    # Regression test: Route.short_name's uniqueness used to be global
    # instead of scoped per transit_system - confirmed live, 67 of 310
    # distinct short_name values are shared by 2-4 different real Bay
    # Area agencies (plain numeric bus route codes like "22" are common
    # across systems). ON CONFLICT (short_name) DO UPDATE doesn't update
    # transit_system_id, so whichever system's fetch ran most recently for
    # a colliding short_name silently stole that row from whichever system
    # owned it first - every other system's own same-numbered route
    # vanished entirely.
    with db_engine.begin() as conn:
        other_transit_system_id = conn.execute(
            insert(TransitSystem)
            .values(
                name="gtfs_test_system_2",
                realtime_url="http://example.com/feed2",
                schedule_url="http://example.com/gtfs2.zip",
                default_schedule_url="http://example.com/routes2",
                last_fetched_at=None,
                timezone=None,
                auth_required=False,
            )
            .returning(TransitSystem.id)
        ).scalar_one()

    try:
        # make_fetcher always writes to the same tmp_path/ROUTES_FILE, so
        # each fetcher's upsert must run (reading the file as it is right
        # now) before the next make_fetcher() call overwrites it.
        fetcher_a = make_fetcher(
            fetcher_module.ROUTES_FILE,
            "route_id,route_short_name,route_long_name,route_color,route_text_color,route_type\n"
            "R1,22,System A's Route 22,FF0000,FFFFFF,3",
        )
        with db_engine.begin() as conn:
            fetcher_a.upsert_routes(conn)

        fetcher_b = make_fetcher(
            fetcher_module.ROUTES_FILE,
            "route_id,route_short_name,route_long_name,route_color,route_text_color,route_type\n"
            "R1,22,System B's Route 22,00FF00,000000,3",
        )
        fetcher_b.transit_system_id = other_transit_system_id
        with db_engine.begin() as conn:
            fetcher_b.upsert_routes(conn)

        with db_engine.begin() as conn:
            rows = conn.execute(
                select(Route).where(Route.short_name == "22")
            ).fetchall()

        assert len(rows) == 2
        by_system = {r.transit_system_id: r.long_name for r in rows}
        assert by_system[transit_system_id] == "System A's Route 22"
        assert by_system[other_transit_system_id] == "System B's Route 22"
    finally:
        with db_engine.begin() as conn:
            conn.execute(
                delete(Route).where(Route.transit_system_id == other_transit_system_id)
            )
            conn.execute(
                delete(TransitSystem).where(TransitSystem.id == other_transit_system_id)
            )
