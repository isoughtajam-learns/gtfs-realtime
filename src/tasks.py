from celery import Celery
from celery.schedules import crontab
from src.fetcher import Fetcher
from src.constants import GTFS_METADATA
from src.settings import get_settings

settings = get_settings()
app = Celery("gtfs_tasks", broker=settings.celery_broker_url)

app.conf.beat_schedule = {
    'fetch-gtfs-schedule-every-4-hours': {
        'task': 'tasks.fetch_all_systems',
        # Every 4 hours starting at 2 AM
        'schedule': crontab(minute=0, hour='2,6,10,14,18,22'),
    },
}
app.conf.timezone = 'UTC'

@app.task
def fetch_all_systems():
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
