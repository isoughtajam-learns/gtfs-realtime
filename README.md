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

  Shares `RealtimeFeedCache` with the SSE loop (see "Rate limiting a source" below) rather than always polling fresh - for most systems (`min_poll_interval_seconds=0`) that's the same as fetching fresh on every request. Looks for an entity matching `trip_id`; `404`s if the trip isn't in the current feed (it may not be running right now), `502`s if the source itself is unreachable or the feed doesn't parse.

  Returns, beyond what the SSE event already has:
  - **Every remaining stop** on the trip (`stops[]`), not just the current one - each with its own arrival/departure time *and* delay, and `schedule_relationship` (e.g. `SKIPPED`).
  - Trip-level `delay`/`timestamp` (overall lateness and feed freshness, distinct from per-stop delay), `start_time`/`start_date`, and `schedule_relationship`.
  - `vehicle_id`/`vehicle_label`, where the source actually publishes a real per-vehicle identifier — verified reliable for MBTA and NY_Waterway; BART's `vehicle.label` is not a vehicle identity (it describes car configuration, e.g. `"3-door"`), and BART never sets `vehicle.id` at all. Helsinki sets neither.
  - Route detail (`route_short_name`/`long_name`/`url`/`color`/`text_color`/`route_type`) and per-stop Schedule detail (`stop_lat`/`stop_lon`, `platform_code`/`platform_name`, `wheelchair_boarding`) sourced from `src/services/trip_detail.py`'s DB lookups, not the realtime feed.

  `route_id` is resolved from **our own stored `Trip.route_id`**, not the live feed's `TripDescriptor.route_id` - some sources (BART) never populate the latter at all, even though the same trip's route is right there in the Schedule data we already ingested. Trusting the live feed for it would silently drop route info for those sources.

- `GET /service_alerts/{transit_system}` — every currently-published GTFS-RT `Alert` for this system (`src/main.py`'s `service_alerts()`, assembly logic in `src/services/service_alerts.py`), hydrated against our stored Schedule data. A distinct feed from `/trip_updates`/`/trip_detail` - fetched from `TransitSystem.alerts_url`, not `realtime_url` - and shares `RealtimeFeedCache` under a `{transit_system}:alerts` key, so a system's `min_poll_interval_seconds` quota (e.g. 511.org) is spent across both feeds together, not budgeted separately. `404`s for an unknown system or one with no `alerts_url` on file (most systems - see "Transit system registry" below); `502`s if the source is unreachable or the feed doesn't parse.

  Only BART and SF-MTA have `alerts_url` populated as of this writing (issue #16 scoped Bay Area only) - Helsinki/MBTA/NY_Waterway all have real, working alerts feeds too, but were deliberately left out of scope.

  Each alert has `cause`/`effect`/`severity_level` (GTFS-RT enum names, e.g. `"MAINTENANCE"`/`"DETOUR"`), `header_text`/`description_text`/`url` (first translation of each, real feeds observed so far only ever publish one), `active_period[]` (start/end unix timestamps, either bound possibly unset), and hydrated `affected_trips[]`/`affected_routes[]`. Hydration is v1-scoped to what an alert names directly via `informed_entity.trip`/`informed_entity.route_id` (plus a named trip's own stored `route_id`, folded in even when the route wasn't named directly) - `informed_entity.stop_id` references aren't hydrated yet.

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

A system's full config lives on its own `TransitSystem` row - `realtime_url`, `alerts_url` (GTFS-RT ServiceAlerts feed - see `GET /service_alerts` above; optional, most systems don't have one on file yet), `schedule_url`, `default_schedule_url` (the `Route.url` fallback for a source that doesn't publish a URL per route), `auth_required` (schema-only for now - see below), `min_poll_interval_seconds` (see "Rate limiting a source" below), and `active`. There's no code-level list of systems anymore (no `GTFS_URLS`/`GTFS_METADATA`/`DEFAULT_SCHEDULE_URL_BY_SYSTEM`, which used to live in `src/constants.py`): `src/services/transit_system_detail.py`'s `get_transit_system_config`/`get_active_transit_systems` are the only reads, and every consumer (`main.py`'s endpoints, `src/tasks.py`'s periodic fetches, `fetcher.py`'s CLI) goes through them.

`active` is deliberately separate from just having URLs on file: a system can be fully configured (real `realtime_url`/`schedule_url`, historical data already ingested) without being served - e.g. a system disabled for reliability reasons keeps its row (and any already-fetched Schedule data) but drops out of `get_active_transit_systems()`/`GET /transit_systems` and 404s from every other endpoint, same as one that was never added at all. **Adding a new system now means inserting a `TransitSystem` row directly** (`active=true`, real `realtime_url`/`schedule_url`) - there's no dict to edit. `auth_required` exists as scaffolding for a future source that needs an API secret to poll `realtime_url`; no current system needs one, so the actual secret lookup/attach at fetch time isn't implemented yet.

### Rate limiting a source

`TransitSystem.min_poll_interval_seconds` (default `0`) bounds how often we make a *real* outbound request to that system's own `realtime_url` - for a source with a strict per-key quota (e.g. 511.org), uncoordinated polling can blow through it fast: `transit_feed()`'s SSE loop polls every 30s *per connected client*, and `/trip_detail` used to fetch fresh on every single request with no throttling at all. `src/services/realtime_feed_cache.py`'s `RealtimeFeedCache` is the shared layer both endpoints go through instead of calling `_fetch_feed` directly - every caller for a given transit_system shares one real fetch, refreshed at most once every `min_poll_interval_seconds`, with a per-system lock so concurrent callers racing a stale cache don't each trigger their own duplicate fetch. `0` means never cache - exactly the old fetch-fresh-always behavior - so this is a no-op for every system that hasn't hit a real rate-limit problem.

Set it directly on the row (no CLI flag yet, unlike `--auth-header`):
```sql
UPDATE transit_system SET min_poll_interval_seconds = 60 WHERE name = 'SomeRateLimitedAgency';
```

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

For a source that requires an API key to download its schedule zip, add `--auth-header 'Header-Name: value'` (repeatable) - e.g. `--auth-header 'Authorization: Bearer xyz'`. A query-param key doesn't need this, just put it in `--schedule-url` directly. This is CLI-only for now: `TransitSystem.auth_required` is schema/API-surface scaffolding (see `src/models.py`) - the automated fetch path (`--all`, celery-beat's scheduled fetches) doesn't look up a stored secret anywhere yet, so `--auth-header` isn't valid with `--all`.

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

Deploys only ever build from a **tagged commit reachable from `origin/main`** - never the local working tree, and never an unmerged branch. Builds go via `git archive <tag> | docker build -` (a clean export of that exact commit's tree) rather than `docker build .` against whatever's on disk.

**The backend and frontend are two fully independent Terraform stacks**, so they can move at their own pace without either blocking or accidentally dragging the other along:
- `gtfs-realtime/deployment` - backend/celery-worker/celery-beat + all the shared infrastructure (EC2 instance, ECS cluster, RDS, ElastiCache, IAM, secrets). **Deploys automatically on every merge to main** (see below).
- `../gtfs-dashboard/deployment` - just the frontend (ECR repo, task definition, service). It looks up the shared ECS cluster/execution role/secrets *by name* (`data` sources), not by reading this stack's state file - the only thing coupling the two is that `var.app_name` must match between them. Still deployed manually (`tag-release.sh` + `deploy.sh`), same as this stack was before automation.

#### Backend: automatic, VERSION-gated

The backend deploys itself - `.github/workflows/deploy.yml`'s `deploy` job runs whenever `.github/workflows/ci.yml`'s `CI` workflow completes successfully on `main` (a `workflow_run` trigger - a job can't `needs:` a job in a different file, so this is the standard way to chain one workflow after another, and it guarantees `lint-and-typecheck`/`test`/`docker-build` all passed on the exact commit before any deploy step runs):

1. Reads the `VERSION` file (plain `X.Y.Z`, no `v` prefix) at repo root, from the exact commit CI just verified (`github.event.workflow_run.head_sha`).
2. **If `vX.Y.Z` is already tagged** (i.e. this merge didn't bump `VERSION`), it skips - no tag, no deploy. This is the normal case for a PR that doesn't warrant a release (docs, CI config, a WIP piece of a larger change).
3. **Otherwise**: tags that commit `vX.Y.Z`, pushes it, builds+pushes the image from that tag, **runs pending migrations as a gating one-off ECS task** (see below), and - only if that succeeds - runs `terraform apply -var backend_image_tag=vX.Y.Z` non-interactively against the shared S3 state (see "Terraform state" below) to actually roll the new image out to the live service.

So the human decision point moved from "confirm before apply" (the old `deploy.sh` prompt) to "bump `VERSION` in the PR" - **`check-version-bump`** (in `ci.yml`, runs on every PR) posts a non-blocking `::warning::` annotation if a PR's `VERSION` matches main's, as a nudge to make that a deliberate choice rather than a silent miss. It doesn't block merging; some PRs genuinely don't need a release.

**Migrations are no longer baked into the backend task definition's own startup command.** They used to be (`alembic upgrade head && uvicorn ...`), which meant a bad migration surfaced as an ECS crash-loop - the container never got past that step, ECS kept retrying it, and the deploy workflow itself reported success even though the release was actually broken. Now `deploy.yml`'s "Run database migration" step clones the *currently-registered* backend task definition (still pointing at the previous release, since this deploy's own `terraform apply` hasn't run yet), swaps in the new image and a migration-only command (`alembic upgrade head`, no port mapping - it doesn't need one), registers that as its own one-off revision, runs it via `aws ecs run-task`, and waits for it to stop. A non-zero exit code fails the deploy right there, before `terraform apply` ever touches the live service - the old release keeps running, untouched, and you get a clean error in the Actions log instead of a crash-loop to diagnose. Local dev (`docker-compose.yml`) keeps the old combined-command convenience; only the production task definition changed.

**The image build is pinned to `linux/arm64`** (`docker buildx build --platform linux/arm64`, via `docker/setup-qemu-action` for cross-arch emulation on GitHub's amd64 runners) - `variables.tf`'s `instance_type` (`t4g.small`) is Graviton, so an `amd64` image crashes on boot with `exec format error`. Confirmed live: the very first automated deploy hit exactly this, because every prior manual deploy had only ever worked by accident - `deploy.sh` never declared a platform either, it just happened to run on an Apple Silicon Mac, which builds `arm64` natively by default. `ci.yml`'s `docker-build` validation job targets the same platform, so an arm64-only build failure (e.g. a dependency with no arm64 wheel) shows up there, not for the first time during a real deploy.

Bump `VERSION` yourself as part of a PR, same semantics as `tag-release.sh` used to apply automatically:
```bash
# e.g. VERSION currently "0.3.5"
echo "0.3.6" > VERSION   # patch: a fix, no new capability
echo "0.4.0" > VERSION   # minor: a new capability, backward compatible
echo "1.0.0" > VERSION   # major: a breaking change
```

CI authenticates to AWS via GitHub OIDC (no static keys stored anywhere) - a dedicated IAM role (`gtfs-realtime-ci-deploy`) trusted only for `token.actions.githubusercontent.com`'s `repo:isoughtajam-learns@196524593/gtfs-realtime@1330525927:ref:refs/heads/main` subject, i.e. only workflow runs triggered by a push to this repo's `main` branch can assume it. (The `@<numeric-id>` suffixes on the org/repo names are GitHub's immutable identifiers, not a typo - confirmed via CloudTrail after the plain `repo:owner/repo:...` format from GitHub's own docs was rejected with a generic "Not authorized" error; the numeric IDs are actually the more robust match since they survive a repo/org rename.) Its ARN is the `AWS_DEPLOY_ROLE_ARN` repo variable (not a secret - an IAM role ARN isn't sensitive on its own; only the OIDC trust condition makes it assumable).

`deployment/deploy.sh`/`tag-release.sh` still work and remain for **manual/fallback use** - an out-of-band hotfix, redeploying an already-tagged version, or debugging the deploy itself. They use your own local AWS credentials against the same remote state the automated deploy uses, so avoid running one while a CI deploy is in flight (see "Terraform state" below).

For the frontend, the pre-automation workflow still applies as-is:
```bash
./deployment/tag-release.sh            # patch bump, e.g. v0.2.1 -> v0.2.2 (run from ../gtfs-dashboard for a frontend release)
cd deployment && ./deploy.sh            # latest tag, or ./deploy.sh v0.2.2 to pin
```
`deploy.sh` builds and pushes the image from its verified tag, runs `terraform plan` against *that stack only*, and asks for confirmation before `terraform apply` rolls out the new task definition. The image is already in ECR by the time you're asked to confirm - answering no only skips the deploy, not the push.

`var.backend_image_tag` / `../gtfs-dashboard`'s `var.frontend_image_tag` have no defaults on purpose - every apply must name an explicit version, so there's no floating `:latest` that could silently drift between what Terraform thinks is deployed and what's actually running (the same class of surprise as the AMI reference in `main.tf` floating to "latest recommended" - see the EC2 instance-replacement note in `deployment/main.tf`'s AMI data source).

This wasn't always two stacks - it started as one Terraform stack managing both, split later via `terraform state rm` + `terraform import` (never destroy/recreate) once independent release cadences made the coupling painful. If you ever need to do something similar: import into the new state first and verify a clean `terraform plan` (zero unexpected diff) *before* removing the resource from the old stack's config/state, so a mistake mid-migration never leaves the resource unowned by either.

#### Terraform state

This stack's state moved from local-only to a remote S3 backend (`deployment/main.tf`'s `terraform { backend "s3" {...} }`) specifically so CI (a fresh VM every run, no access to your laptop's state file) and your own local `deploy.sh`/`tag-release.sh` runs can safely share it. Bucket `gtfs-realtime-tfstate-537735702437` (versioned, encrypted, not public); locking is Terraform's native S3 conditional-write locking (`use_lockfile = true`, needs Terraform >= 1.10 - no separate DynamoDB table). If you ever see a lock-related error running Terraform locally, check whether a CI deploy is currently in progress before assuming it's stuck.

### Alerting (`deployment/alerts.tf`)

Built directly in response to the `v0.3.7` arm64 crash-loop incident, which ran for several minutes before anyone noticed - nothing was watching for it. An SNS topic (`gtfs-realtime-alerts`, `var.alert_email`) gets a CloudWatch alarm notification on either:
- **A service's running task count drops below desired** (`backend`/`celery-worker`/`celery-beat`, via `AWS/ECS`'s `LiveTaskCount` metric - available natively per-service without opting into the extra-billed Container Insights) - sustained for 5 straight 1-minute periods, long enough to ride out a normal deploy's brief 0-task window (this service's single-instance + fixed-host-port setup means the old task must fully stop before the new one starts - see `aws_ecs_service.backend`'s `deployment_minimum_healthy_percent = 0` comment) without false-alarming on every routine release.
- **The EC2 instance itself fails AWS's status checks** (`AWS/EC2`'s `StatusCheckFailed`, system or instance level).

Both fire `ok_actions` too, so you also get notified when it self-recovers, not just when it breaks. **A new `var.alert_email` subscription needs its confirmation link clicked before anything actually gets delivered** - `terraform apply` doesn't wait for that, so check `aws sns list-subscriptions-by-topic --topic-arn $(terraform output -raw alerts_sns_topic_arn)` if you're not sure whether it's live yet.

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