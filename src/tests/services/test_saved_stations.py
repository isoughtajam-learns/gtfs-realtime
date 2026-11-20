from datetime import datetime, timezone
from typing import Generator

import pytest
from sqlalchemy import delete, insert
from sqlalchemy.engine import Engine

from src.models import AppUser, SavedStation
from src.services.saved_stations import (
    MAX_SAVED_STATIONS_PER_SYSTEM,
    SavedStationAlreadyExists,
    SavedStationLimitReached,
    SavedStationNotFound,
    UnknownStopName,
    create_saved_station,
    delete_saved_station,
    list_all_saved_stations,
    list_saved_stations_for_system,
    update_saved_station,
)

KNOWN_STOPS = {"Embarcadero", "Powell St", "Civic Center"}


@pytest.fixture(autouse=True)  # type: ignore[misc]
def _redirect_engine(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.services.saved_stations.engine", db_engine)


@pytest.fixture  # type: ignore[misc]
def user_id(db_engine: Engine) -> Generator[int, None, None]:
    with db_engine.begin() as conn:
        result = conn.execute(
            insert(AppUser)
            .values(
                email="rider+saved_stations@example.com",
                password_hash="hash",
                created_at=datetime.now(timezone.utc),
            )
            .returning(AppUser.id)
        )
        new_id = result.scalar_one()
    yield new_id
    with db_engine.begin() as conn:
        conn.execute(delete(SavedStation).where(SavedStation.user_id == new_id))
        conn.execute(delete(AppUser).where(AppUser.id == new_id))


def test_create_list_update_delete_roundtrip(
    user_id: int, transit_system_id: int
) -> None:
    created = create_saved_station(
        user_id, transit_system_id, "Embarcadero", KNOWN_STOPS, True, 5
    )
    assert created == {
        "stop_name": "Embarcadero",
        "alert_enabled": True,
        "alert_lead_minutes": 5,
    }

    listed = list_saved_stations_for_system(user_id, transit_system_id)
    assert listed == [created]

    all_systems = list_all_saved_stations(user_id)
    assert len(all_systems) == 1
    assert all_systems[0]["stop_name"] == "Embarcadero"

    updated = update_saved_station(user_id, transit_system_id, "Embarcadero", False, 10)
    assert updated == {
        "stop_name": "Embarcadero",
        "alert_enabled": False,
        "alert_lead_minutes": 10,
    }

    delete_saved_station(user_id, transit_system_id, "Embarcadero")
    assert list_saved_stations_for_system(user_id, transit_system_id) == []


def test_create_rejects_unknown_stop_name(user_id: int, transit_system_id: int) -> None:
    with pytest.raises(UnknownStopName):
        create_saved_station(
            user_id, transit_system_id, "Not A Real Stop", KNOWN_STOPS, True, 5
        )


def test_create_rejects_duplicate(user_id: int, transit_system_id: int) -> None:
    create_saved_station(user_id, transit_system_id, "Powell St", KNOWN_STOPS, True, 5)
    with pytest.raises(SavedStationAlreadyExists):
        create_saved_station(
            user_id, transit_system_id, "Powell St", KNOWN_STOPS, True, 5
        )


def test_create_enforces_per_system_cap(user_id: int, transit_system_id: int) -> None:
    known = {f"Stop {i}" for i in range(MAX_SAVED_STATIONS_PER_SYSTEM + 1)}
    for i in range(MAX_SAVED_STATIONS_PER_SYSTEM):
        create_saved_station(user_id, transit_system_id, f"Stop {i}", known, True, 5)

    with pytest.raises(SavedStationLimitReached):
        create_saved_station(
            user_id,
            transit_system_id,
            f"Stop {MAX_SAVED_STATIONS_PER_SYSTEM}",
            known,
            True,
            5,
        )


def test_update_and_delete_raise_not_found_when_missing(
    user_id: int, transit_system_id: int
) -> None:
    with pytest.raises(SavedStationNotFound):
        update_saved_station(user_id, transit_system_id, "Nowhere", True, 5)
    with pytest.raises(SavedStationNotFound):
        delete_saved_station(user_id, transit_system_id, "Nowhere")


def test_saved_stations_are_isolated_per_user(
    db_engine: Engine, transit_system_id: int
) -> None:
    with db_engine.begin() as conn:
        other_user_id = conn.execute(
            insert(AppUser)
            .values(
                email="other+saved_stations@example.com",
                password_hash="hash",
                created_at=datetime.now(timezone.utc),
            )
            .returning(AppUser.id)
        ).scalar_one()
    try:
        create_saved_station(
            other_user_id, transit_system_id, "Civic Center", KNOWN_STOPS, True, 5
        )
        assert list_all_saved_stations(other_user_id + 999999) == []
        with pytest.raises(SavedStationNotFound):
            delete_saved_station(
                other_user_id + 999999, transit_system_id, "Civic Center"
            )
    finally:
        with db_engine.begin() as conn:
            conn.execute(
                delete(SavedStation).where(SavedStation.user_id == other_user_id)
            )
            conn.execute(delete(AppUser).where(AppUser.id == other_user_id))
