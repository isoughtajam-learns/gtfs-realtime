from celery import Celery
from celery.schedules import crontab
from sqlalchemy import select, func

from src.database import engine
from src.commands.fetcher import Fetcher
from src.commands.regional_stop_areas import (
    download_regional_schedule_zip,
    filter_real_station_complexes,
    match_regional_stops,
    parse_stop_areas,
    parse_stop_coordinates,
    upsert_regional_stop_matches,
    upsert_stop_areas,
)
from src.models import TransitSystem, Trip, Stop, Route
from src.services.transit_system_detail import get_active_transit_systems
from src.settings import get_settings
from src.telemetry import configure_posthog_logging

configure_posthog_logging()

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
    "fetch-regional-stop-areas-daily": {
        "task": "src.tasks.fetch_regional_stop_areas",
        # MTC's station-complex groupings (IRL-21/IRL-12) are curated
        # reference data, not a live feed - daily is already far more often
        # than it could plausibly change.
        "schedule": crontab(minute=30, hour=3),
    },
}
app.conf.timezone = "UTC"  # type: ignore[misc,assignment]  # celery-types wrongly requires tzinfo; celery accepts a zone name string


@app.task
def fetch_all_systems() -> None:
    """
    Iterate over all active systems and fetch their metadata updates.
    """
    for system in get_active_transit_systems():
        name, url = system["name"], system["schedule_url"]
        print(f"Fetching GTFS schedule for {name} from {url}")
        fetcher = Fetcher(url, name)
        try:
            fetcher.fetch_metadata_update(force=False)
            print(f"Successfully processed {name}")
        except Exception as e:
            print(f"Error fetching for {name}: {e}")


@app.task
def ensure_schedule_data() -> None:
    """
    Safety net: catches a transit system with no trip/stop/route data at
    all - e.g. a system that was just activated, or a previous fetch that
    upserted transit_system but failed partway through trips/stops/routes
    - and triggers a fetch for it. Route is checked separately from
    trip/stop since a fetch can fully succeed for trips/stops while
    upsert_routes() silently produces zero usable rows for this system
    specifically (confirmed live: Route.short_name's uniqueness being
    global instead of per-transit_system let a colliding short_name from
    another Bay Area agency silently claim every one of this system's
    routes - see the migration that scoped it per-system). Runs far more
    often than fetch_all_systems so a
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
        route_counts: dict[int, int] = {
            transit_system_id: count
            for transit_system_id, count in connection.execute(
                select(Route.transit_system_id, func.count(Route.id)).group_by(
                    Route.transit_system_id
                )
            )
        }

    for system in get_active_transit_systems():
        name, url = system["name"], system["schedule_url"]
        transit_system_id = systems.get(name)
        has_data = (
            transit_system_id is not None
            and trip_counts.get(transit_system_id, 0) > 0
            and stop_counts.get(transit_system_id, 0) > 0
            and route_counts.get(transit_system_id, 0) > 0
        )
        if has_data:
            continue
        print(f"No schedule data found for {name}, triggering fetch")
        fetcher = Fetcher(url, name)
        try:
            # force=True skips should_update() entirely and always
            # downloads+upserts, which is exactly what an empty-DB system
            # needs regardless of what's already promoted on disk.
            fetcher.fetch_metadata_update(force=True)
        except Exception as e:
            print(f"Error fetching for {name}: {e}")


@app.task
def fetch_regional_stop_areas() -> None:
    """Ingests MTC's curated multi-agency station-complex groupings from
    the regional (511.org operator_id=RG) schedule download's
    stop_areas.txt - see models.StopArea's docstring for what this is and
    why it's a separate, additive ingestion from each system's own
    transfers.txt (unaffected by this). Also refreshes RegionalStopMatch,
    the lat/lng crosswalk IRL-25 added for systems (today: just BART) whose
    own stop_ids don't appear verbatim in this feed - reuses the same
    downloaded zip rather than a second request."""
    api_key = get_settings().api_key_511_org
    if not api_key:
        print("API_KEY_511_ORG is not set - skipping regional stop_areas fetch")
        return
    try:
        zip_bytes = download_regional_schedule_zip(api_key)
        rows = filter_real_station_complexes(parse_stop_areas(zip_bytes))
        with engine.begin() as connection:
            total = upsert_stop_areas(connection, rows)
        print(f"upsert stop_areas result: {total} rows")

        member_stop_ids = {row["stop_id"] for row in rows}
        coords = parse_stop_coordinates(zip_bytes, member_stop_ids)
        with engine.begin() as connection:
            match_rows = match_regional_stops(connection, coords)
            match_total = upsert_regional_stop_matches(connection, match_rows)
        print(f"upsert regional_stop_match result: {match_total} rows")
    except Exception as e:
        print(f"Error fetching regional stop_areas: {e}")
