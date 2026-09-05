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
uv run uvicorn src.main:app
```

## Usage
Insert a `TransitSystem` row (`active=true`, a real `realtime_url`) to add a new GTFS-Realtime trip update source - see "GTFS Schedule ingestion" below for the full registry model.

## API endpoints

- `GET /trip_updates/{transit_system}` — SSE stream of live position events (`src/main.py`'s `transit_feed()`). Each event is one trip's *current* stop only - see `/trip_detail` below for the rest of a trip's stops.
- `GET /transit_systems` — names of every active `TransitSystem` row (see `src/services/transit_system_detail.py`).
- `GET /transit_systems/{transit_system}` — system-level metadata that changes rarely (currently just `timezone`, an IANA identifier like `"America/Los_Angeles"` from `agency.txt`'s `agency_timezone`). Its own endpoint rather than a field on every SSE event, since the frontend can fetch and cache it once instead of receiving the same static value on every streamed `trip_update`.
- `GET /trip_detail/{transit_system}/{trip_id}` — everything the realtime feed says about one specific, currently-active trip, plus whatever GTFS Schedule data we have on its route/trip/stops. Meant to back a "trip detail" UI (e.g. clicking an event from the SSE stream).

  Polls the realtime feed fresh on each request (no caching - a detail view is low-frequency, unlike the SSE hot path) and looks for an entity matching `trip_id`; `404`s if the trip isn't in the current feed (it may not be running right now), `502`s if the source itself is unreachable or the feed doesn't parse.

  Returns, beyond what the SSE event already has:
  - **Every remaining stop** on the trip (`stops[]`), not just the current one - each with its own arrival/departure time *and* delay, and `schedule_relationship` (e.g. `SKIPPED`).
  - Trip-level `delay`/`timestamp` (overall lateness and feed freshness, distinct from per-stop delay), `start_time`/`start_date`, and `schedule_relationship`.
  - `vehicle_id`/`vehicle_label`, where the source actually publishes a real per-vehicle identifier — verified reliable for MBTA and NY_Waterway; BART's `vehicle.label` is not a vehicle identity (it describes car configuration, e.g. `"3-door"`), and BART never sets `vehicle.id` at all. Helsinki sets neither.
  - Route detail (`route_short_name`/`long_name`/`url`/`color`/`text_color`/`route_type`) and per-stop Schedule detail (`stop_lat`/`stop_lon`, `platform_code`/`platform_name`, `wheelchair_boarding`) sourced from `src/services/trip_detail.py`'s DB lookups, not the realtime feed.

  `route_id` is resolved from **our own stored `Trip.route_id`**, not the live feed's `TripDescriptor.route_id` - some sources (BART) never populate the latter at all, even though the same trip's route is right there in the Schedule data we already ingested. Trusting the live feed for it would silently drop route info for those sources.

- `GET /info` — app metadata (name, admin email, env, debug mode) from `Settings`.

## Update transit system metadata
```
uv run python -m src.commands.fetcher
```

Pass `--force` to bypass the daily-freshness checks — re-downloads the Schedule zip, replaces `src/tmp/<transit_system>/`, and re-runs the DB upserts even if local data isn't stale:
```
uv run python -m src.commands.fetcher --force
```

Pass `--all` to fetch every active system (see `TransitSystem.active`) instead of the `--transit-system` default (`BART`); one bad/slow feed won't block the others:
```
uv run python -m src.commands.fetcher --all
```

Pass `--diagnose` to download and report on a feed's data quality *without* writing to the database or promoting anything into `src/metadata/` - see "Diagnosing a source" below. Useful before inserting a new system's `TransitSystem` row at all:
```
uv run python -m src.commands.fetcher --diagnose --transit-system BART
uv run python -m src.commands.fetcher --diagnose --transit-system SomeNewAgency --schedule-url https://example.com/gtfs.zip
```

## GTFS Schedule ingestion

The fetcher pulls each transit system's GTFS Schedule zip from its `TransitSystem.schedule_url` row (see "Transit system registry" below), extracts it under `src/tmp/<transit_system>/`, **diagnoses it in place** (see below - this is where `--diagnose` and the automatic pre-promotion gate share the same code path, so the check you can run ahead of time is exactly the check every real fetch runs), promotes the files into `src/metadata/<transit_system>/` only if that diagnosis passes, and upserts the parsed rows into Postgres. Files read: `trips.txt`, `stops.txt`, `stop_times.txt`, `routes.txt`, `feed_info.txt`.

Tables populated (see `src/models.py`):
- `transit_system` — one row per system, holds realtime + schedule URLs, and `timezone` (from `agency.txt`'s `agency_timezone` - see `GET /transit_systems/{transit_system}` above).
- `route` — from `routes.txt`; `route_id`, short/long names, url, colors, `route_type` (GTFS numeric mode enum - bus/rail/ferry/etc).
- `trip` — from `trips.txt`; `trip_id` (string), `route_id`, `direction_id`, `name` (headsign, hydrated via the fallback chain below), `trip_short_name` (rider-facing train/run number, e.g. MBTA commuter rail's "509" - distinct from `trip_id`), `wheelchair_accessible`, `bikes_allowed`.
- `stop` — from `stops.txt` (location_type=0) joined with `stop_times.txt`; captures `stop_headsign`, `lat`/`lon`, `platform_code`/`platform_name`, `wheelchair_boarding` per stop.
- `stop_time` — one row per `stop_times.txt` line (`trip_id`, `stop_sequence`, `stop_id`), indexed on `(transit_system_id, trip_id, stop_sequence)`. Powers `/trip_detail`'s tail-stop backfill (`get_scheduled_tail_stops` in `src/services/trip_detail.py`) with a single indexed query instead of scanning the file per request - see that function's docstring for why (Helsinki's alone is 7.8M rows/~700MB).

All of the fields listed above beyond the original core set (`route_type`, `trip_short_name`, `wheelchair_accessible`, `bikes_allowed`, `lat`/`lon`, `platform_code`/`platform_name`, `wheelchair_boarding`) are optional, same as `zone_id` - present when a source publishes them, `None` otherwise, and never gate whether a row is usable (see `missing_route_fields`/`missing_stop_fields` in `src/services/schedule_utils.py`). They exist to back `GET /trip_detail` (see "API endpoints" above), not the core "show a position on the map" feature.

### Transit system registry

A system's full config lives on its own `TransitSystem` row - `realtime_url`, `schedule_url`, `default_schedule_url` (the `Route.url` fallback for a source that doesn't publish a URL per route), `auth_required` (schema-only for now - see below), and `active`. There's no code-level list of systems anymore (no `GTFS_URLS`/`GTFS_METADATA`/`DEFAULT_SCHEDULE_URL_BY_SYSTEM`, which used to live in `src/constants.py`): `src/services/transit_system_detail.py`'s `get_transit_system_config`/`get_active_transit_systems` are the only reads, and every consumer (`main.py`'s endpoints, `src/tasks.py`'s periodic fetches, `fetcher.py`'s CLI) goes through them.

`active` is deliberately separate from just having URLs on file: a system can be fully configured (real `realtime_url`/`schedule_url`, historical data already ingested) without being served - e.g. a system disabled for reliability reasons keeps its row (and any already-fetched Schedule data) but drops out of `get_active_transit_systems()`/`GET /transit_systems` and 404s from every other endpoint, same as one that was never added at all. **Adding a new system now means inserting a `TransitSystem` row directly** (`active=true`, real `realtime_url`/`schedule_url`) - there's no dict to edit. `auth_required` exists as scaffolding for a future source that needs an API secret to poll `realtime_url`; no current system needs one, so the actual secret lookup/attach at fetch time isn't implemented yet.

### Headsign fallback chain

`Trip.name` is populated greedily so downstream code always has *something* to show. The precedence rule itself lives in `src.services.schedule_utils.resolve_trip_headsign` (a pure function, unit tested in `src/tests/services/test_schedule_utils.py`) - `src/commands/fetcher.py` just calls it:
1. The trip's own destination stop's real name - the stop at its *highest* `stop_sequence` (`schedule_utils.is_later_stop_sequence`), looked up via `stops.stop_name`. `trips.trip_headsign` can be wrong for an irregular run: confirmed live, BART's Saturday through-service trips carry a headsign copied from an unrelated weekday route pattern (a Berryessa-to-Daly-City run labeled "OAK Airport / SF / Daly City") even though the trip's own stop_times.txt sequence correctly shows where it really terminates. Where a train actually stops is never wrong, so it outranks the agency's own asserted headsign.
2. `trips.trip_headsign` - used when this trip has no Schedule stop data of its own to derive a destination from.
3. First non-empty `stop_times.stop_headsign` at the min `stop_sequence` for the trip (`schedule_utils.is_earlier_stop_sequence`)
4. `routes.route_long_name` as a last-ditch fallback
5. `None` if none of the above are available

This is done once at ingest time so the runtime lookup path stays a single dict read. The other ingest-time fallback/completeness rules (route URL fallback, which required route/stop fields must be present to insert a row) live in the same module for the same reason - see its docstrings for the full list.

### Diagnosing a source

`Fetcher.diagnose()` reports whether a GTFS Schedule feed has the data the tables above expect, and if not, exactly where it falls short - per-field counts of what's missing on `routes`/`stops`, which fallback level resolved (or failed to resolve) each trip's headsign, `feed_info.txt` presence/validity, and `stop_times.txt`'s row count (a proxy for memory risk on a small instance). It's read-only: no DB writes, nothing promoted into `src/metadata/`.

This isn't just a manual check - `fetch_metadata_update()` runs the exact same diagnosis on every real fetch, *before* deciding whether to promote `tmp/` into `real_dir()` and upsert. The one hard gate: a source needs at least one derivable stop with a name, or the app's core feature is unavailable for it entirely; anything else (missing route colors, unresolved headsigns, a large `stop_times.txt`) is reported but doesn't block ingestion - partial data is still useful (Helsinki has no route colors at all and stays enabled). `--force` bypasses the daily-freshness gate, not this one.

Run it standalone with `--diagnose` (see above) before inserting a new system's `TransitSystem` row at all - `--schedule-url` lets you point at a feed that isn't registered there yet.

### Runtime hydration (`src/services/schedule_cache.py`)

`ScheduleCache` preloads three per-system dicts on first request and refreshes on a 6-hour TTL:
- `trip_id -> Trip.name`
- `stop_id -> Stop.name`
- `(route_id, direction_id) -> headsign` (with `(route_id, None)` fallback entries) — used when the realtime feed emits a `trip_id` that isn't in the current Schedule (common when an agency renumbers trips between publishes).

`transit_feed()` in `src/main.py` chains these when hydrating each SSE event:
1. `trip_headsigns.get(trip_id)`
2. `headsigns_by_route_dir.get((route_id, direction_id))`
3. Destination stop name — the last `stop_time_update`'s `stop_id` looked up in `stop_names`. Works even when realtime and Schedule share no `trip_id` namespace (e.g. BART).

### Tracking a single trip: the `trip_id` dependency

Everywhere we identify "this train" - `transit_feed()`'s SSE event key, `ScheduleCache` lookups, the `trip`/`stop` tables - is keyed on `trip_update.trip.trip_id` from the realtime feed. That works today for **BART, MBTA, and NY_Waterway**: each publishes a `trip_id` that's unique per entity and stable for the life of the trip (confirmed by polling live, including a stability check across a 15s gap). All three also publish absolute `arrival.time`/`departure.time` on `stop_time_update`, which `get_location()` (`src/services/positioning.py`) requires to place a trip between stops - a source that only publishes relative `arrival.delay` (no absolute `time`) can't be positioned this way at all; this was the reason Estonia was dropped rather than fixed (see `deployment` history / project memory).

**Helsinki_Regional_Transport never sets `trip_update.trip.trip_id`** - it's an empty string on every entity, confirmed live (654/654 entities). HSL instead identifies a trip via the combination of `route_id` + `direction_id` + `start_time` + `start_date`.

`_entity_trip_id()` in `src/main.py` handles this: it uses `trip_update.trip.trip_id` when present, falling back to the top-level `FeedEntity.id` field (distinct from `trip_update.trip.trip_id`, and confirmed live to be both unique per trip and stable across polls - 934/934 unchanged across a 15s gap - for Helsinki) when it's blank. Both `transit_feed()`'s SSE event key and `/trip_detail`'s entity lookup go through this helper, so HSL vehicles no longer collapse into a single row/lookup. It's deliberately *not* used for `ScheduleCache`/DB `trip_id` lookups - a feed entity id was never in Schedule data, so those stay keyed on the raw (possibly blank) descriptor value and degrade to `None`/the destination-stop-name fallback for HSL, same as before.

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
docker compose up --build
```

### Container Dependencies and Startup

The containers are orchestrated to work together:
- The **backend**, **celery-worker**, and **celery-beat** containers all depend on both the **db** and **redis** containers starting first.
- At startup, the **backend** container's command automatically applies any pending Alembic migrations (`alembic upgrade head`) before starting the FastAPI server. It does *not* fetch GTFS Schedule data itself - that's celery-beat's job (see below), or trigger it manually (see "Manually triggering a GTFS Schedule fetch in production"). An earlier version ran the fetcher synchronously at startup; this was removed because it forced every restart to re-download and re-parse every configured system's full Schedule data, rather than respecting the once-daily freshness check.

## Background Tasks (Celery)

We use Celery to periodically fetch fresh GTFS Schedule metadata to keep the database up-to-date.
- **Tasks Definition**: The tasks and schedule are defined in `src/tasks.py`.
- **`fetch_all_systems`**: fetches every active system (see "Transit system registry" above, `force=False`). Runs every 4 hours, starting at 2 AM UTC (hours 2, 6, 10, 14, 18, 22).
- **`ensure_schedule_data`**: safety net. Runs every 15 minutes; checks each configured system's trip+stop counts and triggers a fetch (`force=False`) for any system with none - catches a newly-added system or a fetch that failed partway through before the next scheduled `fetch_all_systems` run. Safe to run this often because of the daily gate below: an already-healthy system costs one indexed SELECT, not a network call.
- **Worker Execution**: The `celery-worker` container listens to the Redis queue and executes tasks, updating the PostgreSQL database.
- **Shared Architecture**: Because they share the same Redis and Database connection strings (passed in `docker-compose.yml`), the worker seamlessly updates the same database queried by the FastAPI server.
- **Daily fetch cap (restart-proof)**: `Fetcher.fetch_metadata_update` won't re-fetch a system more than once per UTC day, gated by `TransitSystem.last_fetched_at` in Postgres - not a local file, which would get wiped on every celery-worker restart/redeploy and defeat the cap. Pass `force=True` to bypass it.
- **Registering a new task**: Celery's auto-generated task name is module-qualified based on how the app is invoked (`celery -A src.tasks worker` → `src.tasks.<funcname>`, not just `tasks.<funcname>`). Verify the actual name in the worker's startup `[tasks]` log banner before wiring it into `beat_schedule` - a mismatched name fails silently (the task is just never dispatched, no error).

# AWS Deployment

### Release & deploy workflow

Deploys only ever build from a **tagged commit reachable from `origin/main`** - never the local working tree, and never an unmerged branch. This is enforced by `deployment/deploy.sh`, not just a convention: it verifies the tag with `git merge-base --is-ancestor` before building, and builds via `git archive <tag> | docker build -` (a clean export of that exact commit's tree) rather than `docker build .` against whatever's on disk.

**The backend and frontend are two fully independent Terraform stacks**, each deployed by its own script, so they can move at their own pace without either blocking or accidentally dragging the other along:
- `gtfs-realtime/deployment` - backend/celery-worker/celery-beat + all the shared infrastructure (EC2 instance, ECS cluster, RDS, ElastiCache, IAM, secrets).
- `../gtfs-dashboard/deployment` - just the frontend (ECR repo, task definition, service). It looks up the shared ECS cluster/execution role/secrets *by name* (`data` sources), not by reading this stack's state file - the only thing coupling the two is that `var.app_name` must match between them.

1. **Merge to `main`** on GitHub as usual (PR workflow), in whichever repo you're releasing.
2. **Cut a release** - tags that repo's `origin/main` tip with the next version and pushes the tag. Defaults to a patch bump:
   ```bash
   ./deployment/tag-release.sh            # patch bump, e.g. v0.2.1 -> v0.2.2
   ./deployment/tag-release.sh minor      # v0.2.2 -> v0.3.0
   ./deployment/tag-release.sh major      # v0.3.0 -> v1.0.0
   ```
   (Same command in `../gtfs-dashboard/deployment/tag-release.sh` for a frontend release.)
3. **Deploy just that side**:
   ```bash
   cd deployment && ./deploy.sh            # latest tag, or ./deploy.sh v0.2.2 to pin
   ```
   Builds and pushes the image from its verified tag, runs `terraform plan` against *that stack only*, and asks for confirmation before `terraform apply` rolls out the new task definition. The image is already in ECR by the time you're asked to confirm - answering no only skips the deploy, not the push.

`var.backend_image_tag` / `../gtfs-dashboard`'s `var.frontend_image_tag` have no defaults on purpose - every apply must name an explicit version, so there's no floating `:latest` that could silently drift between what Terraform thinks is deployed and what's actually running (the same class of surprise as the AMI reference in `main.tf` floating to "latest recommended" - see the EC2 instance-replacement note in `deployment/main.tf`'s AMI data source).

This wasn't always two stacks - it started as one Terraform stack managing both, split later via `terraform state rm` + `terraform import` (never destroy/recreate) once independent release cadences made the coupling painful. If you ever need to do something similar: import into the new state first and verify a clean `terraform plan` (zero unexpected diff) *before* removing the resource from the old stack's config/state, so a mistake mid-migration never leaves the resource unowned by either.

### Manually triggering a GTFS Schedule fetch in production

The backend's startup command only runs `alembic upgrade head` - it does **not** fetch GTFS Schedule data. Schedule data is only populated by celery-beat's `fetch_all_systems` task, scheduled every 4 hours (2/6/10/14/18/22 UTC, see `src/tasks.py`). To populate a system's data immediately instead of waiting for the next scheduled run (e.g. right after activating a new `TransitSystem` row, or after a fix like the one that re-enabled Helsinki):

```bash
ssh -i ~/.ssh/<key-name>.pem ec2-user@<instance-ip>
docker exec $(docker ps -qf name=backend) python -m src.commands.fetcher --transit-system <System_Name> --force
```

`--force` bypasses the once-daily freshness check and always re-downloads + re-parses. Omit it to respect the daily gate (matches what celery-beat does on its own schedule).

To fetch every configured system at once (same as what celery-beat's scheduled task does, but on demand):
```bash
docker exec $(docker ps -qf name=backend) python -m src.commands.fetcher --all --force
```

**Before enabling a new large transit system**, run `--diagnose` first (see "Diagnosing a source" above) - it's quick, needs no DB, and immediately surfaces things like missing route colors or an absent `feed_info.txt`. Then, if `stop_times.txt`'s reported row count looks large, verify actual memory usage locally before trusting it in production - `fetcher.py` holds derived per-trip/per-stop data for the whole file in memory during a fetch:
```bash
docker compose up -d db
docker compose run --rm backend python3 -c "
import resource, time
from src.commands.fetcher import Fetcher
t0 = time.time()
f = Fetcher('<schedule_zip_url>', '<System_Name>')
f.fetch_metadata_update(force=True)
print(f'{time.time()-t0:.1f}s, peak RSS: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.1f} MB')
"
```
(If the system doesn't have a `TransitSystem` row yet, insert one - real `realtime_url`/`schedule_url`, `active=true` - before running this.)

After a manual production fetch, the running backend's in-memory `ScheduleCache` (`src/services/schedule_cache.py`, 6-hour TTL) won't see the new data until its TTL expires. Force a restart to pick it up immediately:
```bash
aws ecs update-service --cluster gtfs-realtime-cluster --service gtfs-realtime-backend --force-new-deployment --region us-east-2
```