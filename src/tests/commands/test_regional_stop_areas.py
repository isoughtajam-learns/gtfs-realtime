# src/tests/commands/test_regional_stop_areas.py
import io
import zipfile

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from src.commands.regional_stop_areas import (
    MAX_REAL_COMPLEX_MEMBERS,
    _haversine_meters,
    filter_real_station_complexes,
    is_real_station_complex,
    match_regional_stops,
    parse_stop_areas,
    parse_stop_coordinates,
    upsert_regional_stop_matches,
    upsert_stop_areas,
)
from src.models import RegionalStopMatch, Stop, StopArea, TransitSystem


def test_is_real_station_complex_accepts_small_mtc_prefixed_area() -> None:
    # Confirmed live: "mtc:powell" (4 members - BART + SF-MTA stops).
    assert is_real_station_complex("mtc:powell", member_count=4)


def test_is_real_station_complex_rejects_non_mtc_prefix() -> None:
    # Confirmed live: fare-zone buckets like "AC:local"/"SC:not-SJC" use a
    # different operator-code prefix, not "mtc:".
    assert not is_real_station_complex("AC:local", member_count=3)


def test_is_real_station_complex_rejects_literal_any() -> None:
    # Confirmed live: "any" is a 21,632-member catch-all fare zone.
    assert not is_real_station_complex("any", member_count=5)


def test_is_real_station_complex_rejects_bare_mtc_prefix() -> None:
    assert not is_real_station_complex("mtc:", member_count=1)


def test_is_real_station_complex_rejects_oversized_mtc_area() -> None:
    assert not is_real_station_complex(
        "mtc:somehow-huge", member_count=MAX_REAL_COMPLEX_MEMBERS + 1
    )


def test_is_real_station_complex_accepts_at_the_member_threshold() -> None:
    assert is_real_station_complex(
        "mtc:at-threshold", member_count=MAX_REAL_COMPLEX_MEMBERS
    )


def _stop_areas_zip(content: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("stop_areas.txt", content)
    return buf.getvalue()


def test_parse_stop_areas_groups_by_area_id() -> None:
    zip_bytes = _stop_areas_zip(
        "area_id,stop_id\nmtc:powell,16995\nmtc:powell,901301\nAC:local,50555\n"
    )

    areas = parse_stop_areas(zip_bytes)

    assert areas == {
        "mtc:powell": ["16995", "901301"],
        "AC:local": ["50555"],
    }


def test_parse_stop_areas_skips_rows_missing_either_column() -> None:
    zip_bytes = _stop_areas_zip(
        "area_id,stop_id\nmtc:powell,16995\nmtc:powell,\n,901301\n"
    )

    areas = parse_stop_areas(zip_bytes)

    assert areas == {"mtc:powell": ["16995"]}


def test_filter_real_station_complexes_drops_fare_zones_keeps_real_complexes() -> None:
    areas = {
        "mtc:powell": ["16995", "901301"],
        "AC:local": [str(n) for n in range(100)],
        "any": [str(n) for n in range(200)],
        "mtc:": ["50000"],
    }

    rows = filter_real_station_complexes(areas)

    assert {(r["area_id"], r["stop_id"]) for r in rows} == {
        ("mtc:powell", "16995"),
        ("mtc:powell", "901301"),
    }


def test_upsert_stop_areas_inserts_rows(db_engine: Engine) -> None:
    rows = [
        {"area_id": "mtc:test-complex", "stop_id": "S1"},
        {"area_id": "mtc:test-complex", "stop_id": "S2"},
    ]
    try:
        with db_engine.begin() as conn:
            total = upsert_stop_areas(conn, rows)
            persisted = conn.execute(
                select(StopArea).where(StopArea.area_id == "mtc:test-complex")
            ).fetchall()

        assert total == 2
        assert {r.stop_id for r in persisted} == {"S1", "S2"}
    finally:
        with db_engine.begin() as conn:
            conn.execute(delete(StopArea).where(StopArea.area_id == "mtc:test-complex"))


def test_upsert_stop_areas_is_idempotent_on_conflict(db_engine: Engine) -> None:
    rows = [{"area_id": "mtc:test-idempotent", "stop_id": "S1"}]
    try:
        with db_engine.begin() as conn:
            upsert_stop_areas(conn, rows)
            upsert_stop_areas(conn, rows)
            persisted = conn.execute(
                select(StopArea).where(StopArea.area_id == "mtc:test-idempotent")
            ).fetchall()

        assert len(persisted) == 1
    finally:
        with db_engine.begin() as conn:
            conn.execute(
                delete(StopArea).where(StopArea.area_id == "mtc:test-idempotent")
            )


# IRL-25: BART's own stop_ids never appear verbatim in the regional feed -
# see models.RegionalStopMatch's docstring - so these cover the lat/lng
# crosswalk that resolves that case instead.


def test_haversine_meters_zero_for_identical_points() -> None:
    assert _haversine_meters(37.6002, -122.38684, 37.6002, -122.38684) == 0.0


def test_haversine_meters_matches_known_distance() -> None:
    # 0.001 degrees of latitude is ~111m at any longitude - a simple,
    # direction-independent sanity check rather than a precise fixture.
    distance = _haversine_meters(37.6002, -122.38684, 37.6012, -122.38684)
    assert 105 < distance < 115


def _stops_zip(content: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("stops.txt", content)
    return buf.getvalue()


def test_parse_stop_coordinates_scopes_to_requested_stop_ids() -> None:
    zip_bytes = _stops_zip(
        "stop_id,stop_lat,stop_lon\n"
        "906403,37.6002,-122.38684\n"
        "70061,37.5994,-122.3873\n"
        "ignored,1.0,1.0\n"
    )

    coords = parse_stop_coordinates(zip_bytes, {"906403", "70061"})

    assert coords == {
        "906403": (37.6002, -122.38684),
        "70061": (37.5994, -122.3873),
    }


def test_parse_stop_coordinates_skips_rows_missing_lat_or_lon() -> None:
    zip_bytes = _stops_zip("stop_id,stop_lat,stop_lon\n906403,,\n")

    assert parse_stop_coordinates(zip_bytes, {"906403"}) == {}


def test_match_regional_stops_finds_nearby_native_stop_within_threshold(
    db_engine: Engine,
) -> None:
    # Mirrors BART's real Millbrae mismatch: a native stop ("BART-MILLBRAE")
    # a few meters from where the regional feed places the same physical
    # stop under its own id ("906403").
    try:
        with db_engine.begin() as conn:
            bart_id = conn.execute(
                insert(TransitSystem)
                .values(
                    name="test_match_bart",
                    realtime_url="http://example.com/bart",
                    active=True,
                )
                .returning(TransitSystem.id)
            ).scalar_one()
            conn.execute(
                insert(Stop).values(
                    stop_id="BART-MILLBRAE",
                    transit_system_id=bart_id,
                    trip_id="t1",
                    name="Millbrae",
                    lat=37.6002,
                    lon=-122.38684,
                )
            )

            result = match_regional_stops(conn, {"906403": (37.60025, -122.38683)})

        assert len(result) == 1
        assert result[0]["transit_system_id"] == bart_id
        assert result[0]["stop_id"] == "BART-MILLBRAE"
        assert result[0]["regional_stop_id"] == "906403"
        assert result[0]["distance_meters"] < 10
    finally:
        with db_engine.begin() as conn:
            conn.execute(delete(Stop).where(Stop.transit_system_id == bart_id))
            conn.execute(delete(TransitSystem).where(TransitSystem.id == bart_id))


def test_match_regional_stops_skips_direct_stop_id_hits(db_engine: Engine) -> None:
    # The common case (8 of 9 active systems today): the regional id already
    # equals a native stop_id, so no crosswalk row is needed at all.
    try:
        with db_engine.begin() as conn:
            caltrain_id = conn.execute(
                insert(TransitSystem)
                .values(
                    name="test_match_caltrain",
                    realtime_url="http://example.com/caltrain",
                    active=True,
                )
                .returning(TransitSystem.id)
            ).scalar_one()
            conn.execute(
                insert(Stop).values(
                    stop_id="70061",
                    transit_system_id=caltrain_id,
                    trip_id="t1",
                    name="Millbrae Caltrain Station Northbound",
                    lat=37.5994,
                    lon=-122.3873,
                )
            )

            result = match_regional_stops(conn, {"70061": (37.5994, -122.3873)})

        assert result == []
    finally:
        with db_engine.begin() as conn:
            conn.execute(delete(Stop).where(Stop.transit_system_id == caltrain_id))
            conn.execute(delete(TransitSystem).where(TransitSystem.id == caltrain_id))


def test_match_regional_stops_ignores_stops_beyond_threshold(db_engine: Engine) -> None:
    try:
        with db_engine.begin() as conn:
            bart_id = conn.execute(
                insert(TransitSystem)
                .values(
                    name="test_match_far",
                    realtime_url="http://example.com/bart",
                    active=True,
                )
                .returning(TransitSystem.id)
            ).scalar_one()
            conn.execute(
                insert(Stop).values(
                    stop_id="BART-FAR",
                    transit_system_id=bart_id,
                    trip_id="t1",
                    name="Somewhere else",
                    lat=37.6002,
                    lon=-122.38684,
                )
            )

            # ~1.1km away (0.01 degrees of latitude) - well past
            # MAX_MATCH_DISTANCE_METERS.
            result = match_regional_stops(conn, {"906403": (37.6102, -122.38684)})

        assert result == []
    finally:
        with db_engine.begin() as conn:
            conn.execute(delete(Stop).where(Stop.transit_system_id == bart_id))
            conn.execute(delete(TransitSystem).where(TransitSystem.id == bart_id))


def test_upsert_regional_stop_matches_inserts_and_updates_on_conflict(
    db_engine: Engine,
) -> None:
    try:
        with db_engine.begin() as conn:
            bart_id = conn.execute(
                insert(TransitSystem)
                .values(
                    name="test_upsert_match",
                    realtime_url="http://example.com/bart",
                    active=True,
                )
                .returning(TransitSystem.id)
            ).scalar_one()

            first = upsert_regional_stop_matches(
                conn,
                [
                    {
                        "transit_system_id": bart_id,
                        "stop_id": "BART-MILLBRAE",
                        "regional_stop_id": "906403",
                        "distance_meters": 40.0,
                    }
                ],
            )
            # A later run finding a closer match should overwrite the
            # stale one, not be silently dropped (unlike upsert_stop_areas'
            # fixed-identity on_conflict_do_nothing).
            second = upsert_regional_stop_matches(
                conn,
                [
                    {
                        "transit_system_id": bart_id,
                        "stop_id": "BART-MILLBRAE",
                        "regional_stop_id": "906403",
                        "distance_meters": 5.0,
                    }
                ],
            )
            persisted = conn.execute(
                select(RegionalStopMatch).where(
                    RegionalStopMatch.transit_system_id == bart_id
                )
            ).fetchall()

        assert first == 1
        assert second == 1
        assert len(persisted) == 1
        assert persisted[0].distance_meters == 5.0
    finally:
        with db_engine.begin() as conn:
            conn.execute(
                delete(RegionalStopMatch).where(
                    RegionalStopMatch.transit_system_id == bart_id
                )
            )
            conn.execute(delete(TransitSystem).where(TransitSystem.id == bart_id))
