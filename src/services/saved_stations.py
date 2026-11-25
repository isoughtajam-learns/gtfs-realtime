"""Phase 4 (.claude/plans/development.md): per-user saved stations - no
cap on how many a user can save per transit system, each with its own
in-app-alert on/off + lead-time setting. See models.SavedStation's own
docstring for why this is keyed by stop_name, not stop_id.

Deliberately plain, sync functions (matching the engine.begin() convention
every other services/*.py module here uses) - validating a stop_name
against a system's current schedule needs ScheduleCache (async), so that
lookup happens in main.py's endpoint and the resulting set of known names
is passed in here, rather than this module reaching into an async cache
itself."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from src.database import engine
from src.models import SavedStation, TransitSystem


class UnknownStopName(Exception):
    pass


class SavedStationAlreadyExists(Exception):
    pass


class SavedStationNotFound(Exception):
    pass


def list_saved_stations_for_system(
    user_id: int, transit_system_id: int
) -> list[dict[str, object]]:
    with engine.begin() as connection:
        rows = connection.execute(
            select(
                SavedStation.stop_name,
                SavedStation.alert_enabled,
                SavedStation.alert_lead_minutes,
            )
            .where(
                SavedStation.user_id == user_id,
                SavedStation.transit_system_id == transit_system_id,
            )
            .order_by(SavedStation.stop_name)
        ).all()
    return [row._asdict() for row in rows]


def list_all_saved_stations(user_id: int) -> list[dict[str, object]]:
    """Backs the Settings page's one-fetch-for-everything view - every
    saved station across every system, with the system's own name joined
    in so the frontend doesn't need a separate /transit_systems round-trip
    per row."""
    with engine.begin() as connection:
        rows = connection.execute(
            select(
                SavedStation.stop_name,
                SavedStation.alert_enabled,
                SavedStation.alert_lead_minutes,
                TransitSystem.name.label("transit_system"),
            )
            .join(TransitSystem, TransitSystem.id == SavedStation.transit_system_id)
            .where(SavedStation.user_id == user_id)
            .order_by(TransitSystem.name, SavedStation.stop_name)
        ).all()
    return [row._asdict() for row in rows]


def create_saved_station(
    user_id: int,
    transit_system_id: int,
    stop_name: str,
    known_stop_names: set[str],
    alert_enabled: bool,
    alert_lead_minutes: int,
) -> dict[str, object]:
    if stop_name not in known_stop_names:
        raise UnknownStopName(stop_name)

    with engine.begin() as connection:
        try:
            connection.execute(
                insert(SavedStation).values(
                    user_id=user_id,
                    transit_system_id=transit_system_id,
                    stop_name=stop_name,
                    alert_enabled=alert_enabled,
                    alert_lead_minutes=alert_lead_minutes,
                    created_at=datetime.now(timezone.utc),
                )
            )
        except IntegrityError as exc:
            raise SavedStationAlreadyExists(stop_name) from exc

    return {
        "stop_name": stop_name,
        "alert_enabled": alert_enabled,
        "alert_lead_minutes": alert_lead_minutes,
    }


def update_saved_station(
    user_id: int,
    transit_system_id: int,
    stop_name: str,
    alert_enabled: Optional[bool],
    alert_lead_minutes: Optional[int],
) -> dict[str, object]:
    values: dict[str, object] = {}
    if alert_enabled is not None:
        values["alert_enabled"] = alert_enabled
    if alert_lead_minutes is not None:
        values["alert_lead_minutes"] = alert_lead_minutes

    with engine.begin() as connection:
        if values:
            # UPDATE...RETURNING in one round trip, same as
            # create_saved_station's insert above - a separate SELECT
            # after the UPDATE would cost an avoidable extra query on every
            # PATCH, and leave a narrow TOCTOU window between the two.
            row = connection.execute(
                update(SavedStation)
                .where(
                    SavedStation.user_id == user_id,
                    SavedStation.transit_system_id == transit_system_id,
                    SavedStation.stop_name == stop_name,
                )
                .values(**values)
                .returning(
                    SavedStation.stop_name,
                    SavedStation.alert_enabled,
                    SavedStation.alert_lead_minutes,
                )
            ).first()
        else:
            # Nothing to update (both fields omitted) - just report the
            # row's current state, the same as a successful update would.
            row = connection.execute(
                select(
                    SavedStation.stop_name,
                    SavedStation.alert_enabled,
                    SavedStation.alert_lead_minutes,
                ).where(
                    SavedStation.user_id == user_id,
                    SavedStation.transit_system_id == transit_system_id,
                    SavedStation.stop_name == stop_name,
                )
            ).first()
    if row is None:
        raise SavedStationNotFound(stop_name)
    return row._asdict()


def delete_saved_station(user_id: int, transit_system_id: int, stop_name: str) -> None:
    with engine.begin() as connection:
        result = connection.execute(
            delete(SavedStation).where(
                SavedStation.user_id == user_id,
                SavedStation.transit_system_id == transit_system_id,
                SavedStation.stop_name == stop_name,
            )
        )
    if result.rowcount == 0:
        raise SavedStationNotFound(stop_name)
