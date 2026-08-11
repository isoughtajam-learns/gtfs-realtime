# Tutorial of gRPC
Supporting tech stack:
- [x] gRPC
- [x] FastAPI
- [x] uv
- [x] precommit w/ ruff & mypy

[Based on tutorial found here](https://gtfs.org/documentation/realtime/language-bindings/python/)

# Install dependencies
```
uv sync
```

# Generate server code from protobuff
```
python -m grpc_tools.protoc -I./protos --python_out=generated/ --pyi_out=generated/ service.proto
python -m grpc_tools.protoc -I./protos --python_out=generated/ --pyi_out=generated/ gtfs-realtime.proto
```
# Run the server
```
uv run fastapi dev
```

## Usage
Update GTFS_URLS in src/constants.py with new GTFS-Realtime trip update sources.

## Update transit system metadata
```
uv run fetcher.py
```

Pass `--force` to bypass the daily-freshness checks — re-downloads the Schedule zip, replaces `src/tmp/<transit_system>/`, and re-runs the DB upserts even if local data isn't stale:
```
uv run fetcher.py --force
```

## GTFS Schedule ingestion

The fetcher pulls each transit system's GTFS Schedule zip from `GTFS_METADATA` in `src/constants.py`, extracts it under `src/tmp/<transit_system>/`, promotes the files into `src/metadata/<transit_system>/` once validated, and upserts the parsed rows into Postgres. Files read: `trips.txt`, `stops.txt`, `stop_times.txt`, `routes.txt`, `feed_info.txt`.

Tables populated (see `src/models.py`):
- `transit_system` — one row per system, holds realtime + schedule URLs.
- `route` — from `routes.txt`; `route_id`, short/long names, url, colors.
- `trip` — from `trips.txt`; `trip_id` (string), `route_id`, `direction_id`, and `name` (headsign) hydrated via the fallback chain below.
- `stop` — from `stops.txt` (location_type=0) joined with `stop_times.txt`; captures `stop_headsign` per stop.

### Headsign fallback chain

`Trip.name` is populated greedily so downstream code always has *something* to show:
1. `trips.trip_headsign` (primary)
2. First non-empty `stop_times.stop_headsign` at the min `stop_sequence` for the trip
3. `routes.route_long_name` as a last-ditch fallback
4. `None` if none of the above are available

This is done once at ingest time so the runtime lookup path stays a single dict read.

### Runtime hydration (`src/schedule_cache.py`)

`ScheduleCache` preloads three per-system dicts on first request and refreshes on a 6-hour TTL:
- `trip_id -> Trip.name`
- `stop_id -> Stop.name`
- `(route_id, direction_id) -> headsign` (with `(route_id, None)` fallback entries) — used when the realtime feed emits a `trip_id` that isn't in the current Schedule (common when an agency renumbers trips between publishes).

`transit_feed()` in `src/main.py` chains these when hydrating each SSE event:
1. `trip_headsigns.get(trip_id)`
2. `headsigns_by_route_dir.get((route_id, direction_id))`
3. Destination stop name — the last `stop_time_update`'s `stop_id` looked up in `stop_names`. Works even when realtime and Schedule share no `trip_id` namespace (e.g. BART).

## Database migrations

Alembic migrations live in `alembic/versions/`. Apply pending migrations before running the fetcher after a schema change:
```
uv run alembic upgrade head
```

## Local Startup and Development (Docker Compose)

This repository includes a `docker-compose.yml` to run the entire backend cluster locally. The cluster consists of:
1. **db**: PostgreSQL database for storing GTFS schedule data.
2. **redis**: Message broker for the Celery task queue.
3. **celery-worker**: Background worker executing tasks from the queue.
4. **celery-beat**: The scheduler that automatically pushes periodic tasks to the queue.
5. **backend**: The FastAPI application server.

To start everything up:
```bash
docker compose up
```

### Container Dependencies and Startup

The containers are orchestrated to work together:
- The **backend**, **celery-worker**, and **celery-beat** containers all depend on both the **db** and **redis** containers starting first.
- At startup, the **backend** container's command automatically applies any pending Alembic migrations (`alembic upgrade head`), and triggers an initial GTFS metadata fetch (`python src/fetcher.py --force`) before starting the FastAPI server. This ensures the database is initialized and seeded with data before the API is accessible.

## Background Tasks (Celery)

We use Celery to periodically fetch fresh GTFS Schedule metadata to keep the database up-to-date.
- **Tasks Definition**: The tasks and schedule are defined in `src/tasks.py`.
- **Schedule**: The `celery-beat` scheduler triggers the `fetch_all_systems` task every 4 hours, starting at 2 AM UTC (i.e. hours 2, 6, 10, 14, 18, 22).
- **Worker Execution**: The `celery-worker` container listens to the Redis queue and executes the task, updating the PostgreSQL database.
- **Shared Architecture**: Because they share the same Redis and Database connection strings (passed in `docker-compose.yml`), the worker seamlessly updates the same database queried by the FastAPI server.