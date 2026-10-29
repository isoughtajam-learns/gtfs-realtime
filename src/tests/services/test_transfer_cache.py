import asyncio

import pytest
from sqlalchemy import delete, insert
from sqlalchemy.engine import Engine

from src.models import RegionalStopMatch, Stop, StopArea, TransitSystem
from src.services.transfer_cache import TransferCache, resolve_inter_system_transfers


@pytest.fixture(autouse=True)  # type: ignore[misc]
def _reset_transfer_cache() -> None:
    # TransferCache is a single global cache (no per-system key, unlike
    # ScheduleCache/RealtimeFeedCache), so it must be reset between tests
    # or an earlier test's data would leak into a later one.
    TransferCache._area_ids_by_stop = {}
    TransferCache._members_by_area = {}
    TransferCache._systems_by_stop = {}
    TransferCache._regional_id_by_native_stop = {}
    TransferCache._loaded_at = None


def test_resolve_inter_system_transfers_finds_other_system_sharing_area() -> None:
    # Mirrors "mtc:powell" (BART's Powell St platforms grouped with SF-MTA's
    # nearby Muni stops) - confirmed live this session.
    area_ids_by_stop = {"BART-POWELL": ["mtc:powell"]}
    members_by_area = {"mtc:powell": ["BART-POWELL", "MUNI-POWELL"]}
    systems_by_stop = {"BART-POWELL": {"BART"}, "MUNI-POWELL": {"SF-MTA"}}

    result = resolve_inter_system_transfers(
        "BART", "BART-POWELL", area_ids_by_stop, members_by_area, systems_by_stop, {}
    )

    assert result == ["SF-MTA"]


def test_resolve_inter_system_transfers_excludes_current_system() -> None:
    area_ids_by_stop = {"A": ["area1"]}
    members_by_area = {"area1": ["A", "B"]}
    systems_by_stop = {"A": {"BART"}, "B": {"BART"}}

    result = resolve_inter_system_transfers(
        "BART", "A", area_ids_by_stop, members_by_area, systems_by_stop, {}
    )

    assert result == []


def test_resolve_inter_system_transfers_empty_when_stop_not_in_any_area() -> None:
    assert resolve_inter_system_transfers("BART", "UNKNOWN", {}, {}, {}, {}) == []


def test_resolve_inter_system_transfers_translates_via_crosswalk() -> None:
    # Mirrors IRL-25: BART's own stop_id ("W40-3") never appears in
    # area_ids_by_stop/members_by_area at all - only the regional feed's own
    # id for that same physical stop ("906403") does. Without the crosswalk
    # translating BART's native id first, this would resolve to nothing.
    area_ids_by_stop = {"906403": ["mtc:millbrae-bart"]}
    members_by_area = {"mtc:millbrae-bart": ["906403", "70061"]}
    systems_by_stop = {"906403": {"BART"}, "70061": {"Caltrain"}}
    regional_id_by_native_stop = {("BART", "W40-3"): "906403"}

    result = resolve_inter_system_transfers(
        "BART",
        "W40-3",
        area_ids_by_stop,
        members_by_area,
        systems_by_stop,
        regional_id_by_native_stop,
    )

    assert result == ["Caltrain"]


def test_transfer_cache_load_builds_cross_system_index(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TransferCache.get() only considers *active* systems (see its query's
    # TransitSystem.active filter) - the shared transit_system_id fixture's
    # system defaults to inactive, so this test creates its own pair of
    # active systems instead of reusing that fixture, and tears them down
    # itself since they're outside _clean_schedule_tables' scope.
    #
    # TransferCache._load() opens its own connection from the module-level
    # `engine` (unlike query_intra_transfers, which takes a Connection the
    # caller already has) - repointed at this test's own Postgres container
    # so _load() reads what this test just inserted, not whatever
    # src.database.engine's real DATABASE_URL points to.
    monkeypatch.setattr("src.services.transfer_cache.engine", db_engine)

    with db_engine.begin() as conn:
        bart_id = conn.execute(
            insert(TransitSystem)
            .values(
                name="test_bart",
                realtime_url="http://example.com/bart",
                active=True,
            )
            .returning(TransitSystem.id)
        ).scalar_one()
        muni_id = conn.execute(
            insert(TransitSystem)
            .values(
                name="test_sf_mta",
                realtime_url="http://example.com/muni",
                active=True,
            )
            .returning(TransitSystem.id)
        ).scalar_one()
        conn.execute(
            insert(Stop).values(
                stop_id="BART-POWELL",
                transit_system_id=bart_id,
                trip_id="t1",
                name="Powell St",
            )
        )
        conn.execute(
            insert(Stop).values(
                stop_id="MUNI-POWELL",
                transit_system_id=muni_id,
                trip_id="t2",
                name="Powell St Station",
            )
        )
        conn.execute(
            insert(StopArea).values(area_id="mtc:powell", stop_id="BART-POWELL")
        )
        conn.execute(
            insert(StopArea).values(area_id="mtc:powell", stop_id="MUNI-POWELL")
        )
        # IRL-25's crosswalk: a stop whose native id ("BART-MILLBRAE")
        # differs from what the regional feed itself calls that stop
        # ("906403" - the same shape as BART's real Millbrae mismatch).
        conn.execute(
            insert(Stop).values(
                stop_id="BART-MILLBRAE",
                transit_system_id=bart_id,
                trip_id="t3",
                name="Millbrae",
            )
        )
        conn.execute(
            insert(StopArea).values(area_id="mtc:millbrae-bart", stop_id="906403")
        )
        conn.execute(
            insert(StopArea).values(
                area_id="mtc:millbrae-bart", stop_id="MUNI-MILLBRAE"
            )
        )
        conn.execute(
            insert(RegionalStopMatch).values(
                transit_system_id=bart_id,
                stop_id="BART-MILLBRAE",
                regional_stop_id="906403",
                distance_meters=12.5,
            )
        )
        conn.execute(
            insert(Stop).values(
                stop_id="MUNI-MILLBRAE",
                transit_system_id=muni_id,
                trip_id="t4",
                name="Millbrae",
            )
        )

    try:
        (
            area_ids_by_stop,
            members_by_area,
            systems_by_stop,
            regional_id_by_native_stop,
        ) = asyncio.run(TransferCache.get())

        assert area_ids_by_stop["BART-POWELL"] == ["mtc:powell"]
        assert set(members_by_area["mtc:powell"]) == {"BART-POWELL", "MUNI-POWELL"}
        assert systems_by_stop["BART-POWELL"] == {"test_bart"}
        assert systems_by_stop["MUNI-POWELL"] == {"test_sf_mta"}

        result = resolve_inter_system_transfers(
            "test_bart",
            "BART-POWELL",
            area_ids_by_stop,
            members_by_area,
            systems_by_stop,
            regional_id_by_native_stop,
        )
        assert result == ["test_sf_mta"]

        # The crosswalk path: BART-MILLBRAE never appears in
        # area_ids_by_stop directly (only the regional feed's "906403"
        # does), but the loaded regional_id_by_native_stop should still
        # resolve it end to end.
        assert regional_id_by_native_stop[("test_bart", "BART-MILLBRAE")] == "906403"
        assert systems_by_stop["906403"] == {"test_bart"}
        millbrae_result = resolve_inter_system_transfers(
            "test_bart",
            "BART-MILLBRAE",
            area_ids_by_stop,
            members_by_area,
            systems_by_stop,
            regional_id_by_native_stop,
        )
        assert millbrae_result == ["test_sf_mta"]
    finally:
        with db_engine.begin() as conn:
            conn.execute(
                delete(RegionalStopMatch).where(
                    RegionalStopMatch.transit_system_id == bart_id
                )
            )
            conn.execute(
                delete(StopArea).where(
                    StopArea.area_id.in_(["mtc:powell", "mtc:millbrae-bart"])
                )
            )
            conn.execute(
                delete(Stop).where(Stop.transit_system_id.in_([bart_id, muni_id]))
            )
            conn.execute(
                delete(TransitSystem).where(TransitSystem.id.in_([bart_id, muni_id]))
            )
