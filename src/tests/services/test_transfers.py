from sqlalchemy import insert
from sqlalchemy.engine import Engine

from src.models import Route, Transfer
from src.services.transfers import query_intra_transfers


def _insert_route(
    db_engine: Engine, transit_system_id: int, route_id: str, short_name: str
) -> None:
    with db_engine.begin() as conn:
        conn.execute(
            insert(Route).values(
                transit_system_id=transit_system_id,
                route_id=route_id,
                short_name=short_name,
                long_name=f"{short_name} Long",
                url=None,
                color="112233",
                text_color="ffffff",
                route_type=1,
            )
        )


def _insert_transfer(
    db_engine: Engine,
    transit_system_id: int,
    from_stop_id: str,
    to_stop_id: str,
    to_route_id: str | None,
) -> None:
    with db_engine.begin() as conn:
        conn.execute(
            insert(Transfer).values(
                transit_system_id=transit_system_id,
                from_stop_id=from_stop_id,
                to_stop_id=to_stop_id,
                from_route_id=None,
                to_route_id=to_route_id,
                from_trip_id=None,
                to_trip_id=None,
                transfer_type=0,
                min_transfer_time=None,
            )
        )


def test_query_intra_transfers_joins_route_for_display(
    db_engine: Engine, transit_system_id: int
) -> None:
    _insert_route(db_engine, transit_system_id, "ROUTE-7", "7")
    _insert_transfer(db_engine, transit_system_id, "STOP-A", "STOP-B", "ROUTE-7")

    with db_engine.begin() as conn:
        result = query_intra_transfers(conn, transit_system_id)

    assert len(result["STOP-A"]) == 1
    transfer = result["STOP-A"][0]
    assert transfer.route_id == "ROUTE-7"
    assert transfer.route_short_name == "7"
    assert transfer.color == "112233"
    assert transfer.text_color == "ffffff"


def test_query_intra_transfers_skips_rows_with_no_to_route_id(
    db_engine: Engine, transit_system_id: int
) -> None:
    # Allowed by the GTFS spec (a transfer can apply generally at a stop
    # rather than scoping to one route) - nothing to label a pill with, so
    # it must not surface as a bare/empty entry.
    _insert_transfer(db_engine, transit_system_id, "STOP-A", "STOP-A", None)

    with db_engine.begin() as conn:
        result = query_intra_transfers(conn, transit_system_id)

    assert result == {}


def test_query_intra_transfers_can_scope_to_specific_stop_ids(
    db_engine: Engine, transit_system_id: int
) -> None:
    _insert_route(db_engine, transit_system_id, "ROUTE-7", "7")
    _insert_route(db_engine, transit_system_id, "ROUTE-9", "9")
    _insert_transfer(db_engine, transit_system_id, "STOP-A", "STOP-B", "ROUTE-7")
    _insert_transfer(db_engine, transit_system_id, "STOP-C", "STOP-D", "ROUTE-9")

    with db_engine.begin() as conn:
        result = query_intra_transfers(conn, transit_system_id, stop_ids=["STOP-A"])

    assert set(result.keys()) == {"STOP-A"}


def test_query_intra_transfers_empty_for_unknown_system(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        result = query_intra_transfers(conn, transit_system_id=-1)

    assert result == {}
