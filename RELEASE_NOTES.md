# v0.2.0

_Covers all uncommitted changes as of 2026-08-15, relative to `656b735` (Update readme with details for wider consumption)._

### AWS Deployment

- **New Terraform stack** (`deployment/main.tf`, `variables.tf`, `outputs.tf`): a single `t4g.small` EC2 instance running ECS (EC2 launch type, `host` networking), registered into an ECS cluster and fronted by a Terraform-managed Elastic IP. Runs four services from two ECR repos:
  - `backend`, `celery-worker`, `celery-beat` — the single image this repo's `Dockerfile` builds.
  - `frontend` — the dashboard from the sibling `../gtfs-dashboard` repo (its own `Dockerfile`, its own ECR repo).
  - Postgres and Redis are RDS and ElastiCache, not containers — container storage isn't durable on this compute model.
  - Secrets (`DATABASE_URL`, app `SECRET_KEY`) are generated and injected via Secrets Manager, not plaintext environment variables.
- **`deployment/deploy.sh`** (new): builds and pushes both the backend and frontend images, then rolls out all four ECS services with `force-new-deployment`.
- **`.gitignore`**: excludes Terraform local state/cache (`deployment/.terraform/`, `*.tfstate*`, `tfplan*`, `*.tfvars`) — these contain the RDS password and app secret key in plaintext and must never be committed.
- **README**: added an AWS deployment / operations section covering manual GTFS Schedule fetches in production, local memory-profiling before enabling a new large transit system, and Celery task-naming gotchas (see below).

### GTFS Fetcher (`src/fetcher.py`)

- **Fixed a silent data-loss bug**: `upsert_trips`, `upsert_stops`, and `upsert_routes` each ended with `print(f"... {result.keys()}")` against a bulk multi-row insert result, which SQLAlchemy rejects (`ResourceClosedError`). Since all three ran inside one `try/except`, the crash on `upsert_trips`'s own print silently aborted `upsert_stops`/`upsert_routes` for **every** transit system, every time — trips were written, stops and routes never were. Fixed by reporting `rowcount` instead.
- **Fixed a memory-exhaustion bug on large sources**: `upsert_trips` and `upsert_stops` each independently parsed the entirety of `stop_times.txt` — by far the largest GTFS file (one row per stop visit per trip). Reading it twice, on every fetch, exhausted memory on large systems. Replaced with `_scan_stop_times()`, a single streaming pass shared by both callers. Verified against real Helsinki (HSL) data: peak memory dropped from 1699.5 MB to 353.7 MB (~4.8x) for the same 341,119 trips / 8,241 stops.
- **Batched upserts** (`_upsert_batched`, 2000 rows/batch) for trips, stops, and routes, so no single transit system — however large — builds one enormous SQL statement, regardless of size.
- **`--all` CLI flag**: fetch every configured system in one invocation instead of only the `--transit-system` default (`BART`). Each system is wrapped in its own `try/except` so one bad or slow feed doesn't block the rest.
- **`timeout=60`** added to the schedule-zip download request (previously unbounded).
- **New durable daily-fetch gate**: `TransitSystem.last_fetched_at` (new column, see Database below) is checked via `Fetcher.fetched_today()` before any network activity. The previous gate (`updated_recently()`) lived in a local file inside the celery-worker container's ephemeral filesystem, which is wiped on every restart/redeploy — meaning it could not actually prevent repeat same-day fetches across container churn. The new DB-backed check can, and was verified to do so across a fresh, stateless container.

### Celery (`src/tasks.py`)

- **Fixed the scheduled fetch task not running at all**: `beat_schedule` referenced the task as `'tasks.fetch_all_systems'`, but Celery's actual registered name (confirmed via the worker's startup banner) is `'src.tasks.fetch_all_systems'`, since the worker is invoked as `celery -A src.tasks worker`. A scheduled task dispatched under an unregistered name is silently dropped — this had very likely never successfully executed in this deployment.
- **New `ensure_schedule_data` task**, scheduled every 15 minutes: checks trip and stop counts per configured system and triggers a fetch for anything empty. Safe to run this often specifically because of `fetched_today()` above — a healthy system costs one indexed `SELECT`, not a network request.

### Database

- **New migration** `f5a6b7c8d9e0_add_transit_system_last_fetched_at.py`: adds nullable `transit_system.last_fetched_at`.

### Transit systems (`src/constants.py`)

- Added `Eurostar` to `GTFS_URLS`/`GTFS_METADATA`.
- Added a `DEFAULT_SCHEDULE_URL_BY_SYSTEM` entry for `Helsinki_Regional_Transport`.
- (Helsinki was temporarily disabled and re-enabled during this same round of work, once the memory-exhaustion bug above was fixed — no net change from its prior enabled state.)

### Local development

- `docker-compose.yml`: the `backend` service no longer runs `python src/fetcher.py --force` as part of its startup command — startup is now just `alembic upgrade head && uvicorn ...`. The forced fetch on every restart re-downloaded and re-parsed every configured system's full Schedule data regardless of freshness; schedule population is now solely `celery-beat`'s job (or triggered manually — see README).
- `docker-compose.yml`: `redis` service given an explicit `container_name: gtfs-realtime-redis`.

### Known gaps (not addressed in this round, flagged for follow-up)

- **HSL (Helsinki) publishes no `route_color`/`route_text_color` columns** in its `routes.txt` at all. `upsert_routes` requires all fields present, so Helsinki's routes — and therefore trip-update colors — never populate. This is an upstream data gap, not a bug; fixing it means choosing a fallback color (a product decision), not just a code change.
- **`Provence-Alpes` and `Eurostar` both lack a `feed_info.txt`** in their GTFS Schedule zips, so `should_update()` reports "no update needed" even with zero local data, and they never populate under normal (`force=False`) operation. `--force` bypasses this. Whether a missing `feed_info.txt` should be treated as "always needs updating" is an open question.
- Staggering the several configured systems' fetches across time (rather than all firing within the same task invocation) was discussed as a possible follow-up; not needed for now given the batching/streaming fixes above.
