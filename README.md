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
Update GTFS_URLS in src/constants.py with new GTFS-Realtime trip update sources.

## Update transit system metadata
```
uv run python -m src.commands.fetcher
```

Pass `--force` to bypass the daily-freshness checks — re-downloads the Schedule zip, replaces `src/tmp/<transit_system>/`, and re-runs the DB upserts even if local data isn't stale:
```
uv run python -m src.commands.fetcher --force
```

Pass `--all` to fetch every system in `GTFS_METADATA` instead of the `--transit-system` default (`BART`); one bad/slow feed won't block the others:
```
uv run python -m src.commands.fetcher --all
```

Pass `--diagnose` to download and report on a feed's data quality *without* writing to the database or promoting anything into `src/metadata/` - see "Diagnosing a source" below. Useful before adding a new system to `GTFS_METADATA` at all:
```
uv run python -m src.commands.fetcher --diagnose --transit-system BART
uv run python -m src.commands.fetcher --diagnose --transit-system SomeNewAgency --schedule-url https://example.com/gtfs.zip
```

## GTFS Schedule ingestion

The fetcher pulls each transit system's GTFS Schedule zip from `GTFS_METADATA` in `src/constants.py`, extracts it under `src/tmp/<transit_system>/`, **diagnoses it in place** (see below - this is where `--diagnose` and the automatic pre-promotion gate share the same code path, so the check you can run ahead of time is exactly the check every real fetch runs), promotes the files into `src/metadata/<transit_system>/` only if that diagnosis passes, and upserts the parsed rows into Postgres. Files read: `trips.txt`, `stops.txt`, `stop_times.txt`, `routes.txt`, `feed_info.txt`.

Tables populated (see `src/models.py`):
- `transit_system` — one row per system, holds realtime + schedule URLs.
- `route` — from `routes.txt`; `route_id`, short/long names, url, colors.
- `trip` — from `trips.txt`; `trip_id` (string), `route_id`, `direction_id`, and `name` (headsign) hydrated via the fallback chain below.
- `stop` — from `stops.txt` (location_type=0) joined with `stop_times.txt`; captures `stop_headsign` per stop.

### Headsign fallback chain

`Trip.name` is populated greedily so downstream code always has *something* to show. The precedence rule itself lives in `src.services.schedule_utils.resolve_trip_headsign` (a pure function, unit tested in `src/tests/services/test_schedule_utils.py`) - `src/commands/fetcher.py` just calls it:
1. `trips.trip_headsign` (primary)
2. First non-empty `stop_times.stop_headsign` at the min `stop_sequence` for the trip (`schedule_utils.is_earlier_stop_sequence`)
3. `routes.route_long_name` as a last-ditch fallback
4. `None` if none of the above are available

This is done once at ingest time so the runtime lookup path stays a single dict read. The other ingest-time fallback/completeness rules (route URL fallback, which required route/stop fields must be present to insert a row) live in the same module for the same reason - see its docstrings for the full list.

### Diagnosing a source

`Fetcher.diagnose()` reports whether a GTFS Schedule feed has the data the tables above expect, and if not, exactly where it falls short - per-field counts of what's missing on `routes`/`stops`, which fallback level resolved (or failed to resolve) each trip's headsign, `feed_info.txt` presence/validity, and `stop_times.txt`'s row count (a proxy for memory risk on a small instance). It's read-only: no DB writes, nothing promoted into `src/metadata/`.

This isn't just a manual check - `fetch_metadata_update()` runs the exact same diagnosis on every real fetch, *before* deciding whether to promote `tmp/` into `real_dir()` and upsert. The one hard gate: a source needs at least one derivable stop with a name, or the app's core feature is unavailable for it entirely; anything else (missing route colors, unresolved headsigns, a large `stop_times.txt`) is reported but doesn't block ingestion - partial data is still useful (Helsinki has no route colors at all and stays enabled). `--force` bypasses the daily-freshness gate, not this one.

Run it standalone with `--diagnose` (see above) before adding a new system to `GTFS_METADATA` at all - `--schedule-url` lets you point at a feed that isn't registered there yet.

### Runtime hydration (`src/services/schedule_cache.py`)

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
docker compose up --build
```

### Container Dependencies and Startup

The containers are orchestrated to work together:
- The **backend**, **celery-worker**, and **celery-beat** containers all depend on both the **db** and **redis** containers starting first.
- At startup, the **backend** container's command automatically applies any pending Alembic migrations (`alembic upgrade head`) before starting the FastAPI server. It does *not* fetch GTFS Schedule data itself - that's celery-beat's job (see below), or trigger it manually (see "Manually triggering a GTFS Schedule fetch in production"). An earlier version ran the fetcher synchronously at startup; this was removed because it forced every restart to re-download and re-parse every configured system's full Schedule data, rather than respecting the once-daily freshness check.

## Background Tasks (Celery)

We use Celery to periodically fetch fresh GTFS Schedule metadata to keep the database up-to-date.
- **Tasks Definition**: The tasks and schedule are defined in `src/tasks.py`.
- **`fetch_all_systems`**: fetches every system in `GTFS_METADATA` (`force=False`). Runs every 4 hours, starting at 2 AM UTC (hours 2, 6, 10, 14, 18, 22).
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

The backend's startup command only runs `alembic upgrade head` - it does **not** fetch GTFS Schedule data. Schedule data is only populated by celery-beat's `fetch_all_systems` task, scheduled every 4 hours (2/6/10/14/18/22 UTC, see `src/tasks.py`). To populate a system's data immediately instead of waiting for the next scheduled run (e.g. right after adding a new `GTFS_URLS`/`GTFS_METADATA` entry, or after a fix like the one that re-enabled Helsinki):

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
(If the system isn't in `GTFS_URLS`/`GTFS_METADATA` yet, monkey-patch those dicts in the same script before importing `Fetcher` - see the fetch that validated Helsinki for the pattern.)

After a manual production fetch, the running backend's in-memory `ScheduleCache` (`src/services/schedule_cache.py`, 6-hour TTL) won't see the new data until its TTL expires. Force a restart to pick it up immediately:
```bash
aws ecs update-service --cluster gtfs-realtime-cluster --service gtfs-realtime-backend --force-new-deployment --region us-east-2
```