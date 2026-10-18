# src/tests/commands/test_regional_stop_areas.py
import io
import zipfile

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from src.commands.regional_stop_areas import (
    MAX_REAL_COMPLEX_MEMBERS,
    filter_real_station_complexes,
    is_real_station_complex,
    parse_stop_areas,
    upsert_stop_areas,
)
from src.models import StopArea


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
