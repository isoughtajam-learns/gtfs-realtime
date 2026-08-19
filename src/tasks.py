from celery import Celery
from celery.schedules import crontab
from sqlalchemy import select, func

from src.database import engine
from src.commands.fetcher import Fetcher
from src.constants import GTFS_METADATA
from src.models import TransitSystem, Trip, Stop
from src.settings import get_settings

settings = get_settings()
app = Celery("gtfs_tasks", broker=settings.celery_broker_url)

app.conf.beat_schedule = {
    "fetch-gtfs-schedule-every-4-hours": {
        "task": "src.tasks.fetch_all_systems",
        # Every 4 hours starting at 2 AM
        "schedule": crontab(minute=0, hour="2,6,10,14,18,22"),
    },
    "ensure-schedule-data-populated": {
        "task": "src.tasks.ensure_schedule_data",
        # Runs often to catch a system with no data quickly, but each fetch
        # attempt is a cheap no-op once TransitSystem.last_fetched_at is set
        # for today (see Fetcher.fetched_today), so frequent runs are safe.
        "schedule": crontab(minute="*/15"),
    },
}
app.conf.timezone = "UTC"  # type: ignore[misc,assignment]  # celery-types wrongly requires tzinfo; celery accepts a zone name string


@app.task
def fetch_all_systems() -> None:
    """
    Iterate over all systems and fetch their metadata updates.
    """
    for system, url in GTFS_METADATA.items():
        print(f"Fetching GTFS schedule for {system} from {url}")
        fetcher = Fetcher(url, system)
        try:
            fetcher.fetch_metadata_update(force=False)
            print(f"Successfully processed {system}")
        except Exception as e:
            print(f"Error fetching for {system}: {e}")


@app.task
def ensure_schedule_data() -> None:
    """
    Safety net: catches a transit system with no trip/stop data at all - e.g.
    a system that was just added to GTFS_METADATA, or a previous fetch that
    upserted transit_system but failed partway through trips/stops - and
    triggers a fetch for it. Runs far more often than fetch_all_systems so a
    broken/empty system gets caught quickly, but Fetcher.fetched_today makes
    repeat calls for an already-healthy system a cheap no-op (single indexed
    SELECT, no network request), so running this frequently doesn't hammer
    upstream GTFS endpoints.
    """
    with engine.begin() as connection:
        systems: dict[str, int] = {
            name: transit_system_id
            for name, transit_system_id in connection.execute(
                select(TransitSystem.name, TransitSystem.id)
            )
        }
        trip_counts: dict[int, int] = {
            transit_system_id: count
            for transit_system_id, count in connection.execute(
                select(Trip.transit_system_id, func.count(Trip.trip_id)).group_by(
                    Trip.transit_system_id
                )
            )
        }
        stop_counts: dict[int, int] = {
            transit_system_id: count
            for transit_system_id, count in connection.execute(
                select(Stop.transit_system_id, func.count(Stop.stop_id)).group_by(
                    Stop.transit_system_id
                )
            )
        }

    for system, url in GTFS_METADATA.items():
        transit_system_id = systems.get(system)
        has_data = (
            transit_system_id is not None
            and trip_counts.get(transit_system_id, 0) > 0
            and stop_counts.get(transit_system_id, 0) > 0
        )
        if has_data:
            continue
        print(f"No schedule data found for {system}, triggering fetch")
        fetcher = Fetcher(url, system)
        try:
            fetcher.fetch_metadata_update(force=False)
        except Exception as e:
            print(f"Error fetching for {system}: {e}")
