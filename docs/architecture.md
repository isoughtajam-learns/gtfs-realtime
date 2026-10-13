# Architecture

## Overview

`gtfs-realtime` is a FastAPI service with one external job: take live GTFS-Realtime
feeds and static GTFS Schedule data from several transit agencies, and serve a unified,
enriched view of "what's happening right now" to a frontend. There's no database writes
from end users — the only writer is the ingestion pipeline (Celery tasks pulling static
Schedule data, and the live SSE loop pulling realtime feeds); everything the API serves
is either a direct pass-through of a live feed or a DB read of previously-ingested
Schedule data.

## Request flow for the main endpoint

`GET /trip_updates/{transit_system}` (`src/main.py`'s `transit_feed()`) is a long-lived
Server-Sent-Events connection. Each connected client runs an independent polling loop
(every 30s) that:

1. Fetches the system's live GTFS-RT feed (through `RealtimeFeedCache` — see below).
2. For each `trip_update` entity, resolves a destination headsign, route colors,
   `route_short_name`/`route_long_name`, and a stop name — all from an in-memory
   Schedule cache (see below), since the realtime feed itself only supplies bare IDs
   (`trip_id`, `stop_id`, sometimes `route_id`).
3. Emits one `TripPosition` event per active trip, and updates `RecentEventsCache`.

A new client is sent `RecentEventsCache`'s most-recently-updated events immediately on
connect, before its first real poll — so there's something to show right away instead of
waiting out a slow system's poll interval.

## The three caching/coordination layers

These exist because a naive "poll the live feed on every client's every tick" design
breaks down once (a) multiple clients connect to the same system, and (b) some upstream
sources enforce strict per-key rate limits. Each layer solves a different problem:

### `RealtimeFeedCache` (`src/services/realtime_feed_cache.py`)

Shares one real outbound fetch across every connected client of a given system, subject
to `TransitSystem.min_poll_interval_seconds` (0 = never cache, the default — matches
pre-rate-limiting behavior for systems with no quota concerns). Without this, N
connected clients to the same system would mean N× the outbound request rate — the
upstream source has no idea multiple people are watching the same data.

### `ScheduleCache` (`src/services/schedule_cache.py`)

Preloads per-system lookups (trip→headsign, stop→name, route→color/short_name/long_name,
and a few fallback-chain variants of each) from the database once, with a TTL refresh
(Schedule data changes at most daily). Without this, every single SSE event would need a
DB round-trip to resolve a human-readable name — expensive at the polling cadence this
app runs at.

Each lookup generally has a *trip-level* and a *route-level* variant, tried in that
order. This exists because some sources (confirmed for BART) leave `TripDescriptor.route_id`
blank on the live feed even though the trip's route is known from Schedule data — the
trip-level lookup (keyed by the trip's own stored `route_id`, independent of what the live
entity says) papers over that gap.

### `SharedFeedQuota` + `QuotaGroupScheduler` (`src/services/shared_feed_quota.py`,
`src/services/quota_group_scheduler.py`)

A `TransitSystem.quota_group` groups several systems that share one upstream rate limit
(e.g. every 511.org-backed system shares one API key's hourly budget, not one budget
each). See [`rate-limiting-and-scheduling.md`](rate-limiting-and-scheduling.md) for the
full design — it's involved enough to warrant its own doc.

## Data model

- `TransitSystem` — one row per onboarded agency: name, realtime/alerts/schedule feed
  URLs, `active` flag, `min_poll_interval_seconds`, `quota_group`, `auth_required`,
  `timezone` (from `agency.txt`). This is the registry; there's no separate config file
  listing active systems — adding a system means inserting a row via migration.
- `Route`, `Trip`, `Stop`, `StopTime`, `Transfer` — static GTFS Schedule data, re-ingested
  periodically (see the Celery pipeline below). Keyed per `transit_system_id` (except
  `Route.short_name`, which is a *global* unique column across all systems — see
  `incidents-and-lessons.md` for why that mattered).

## Background ingestion (Celery)

`src/tasks.py` runs a periodic (`celery-beat`) task, `fetch_all_systems`, that re-pulls
every active system's GTFS Schedule zip and upserts it via `src/commands/fetcher.py`.
A separate, more frequent task (`ensure_schedule_data`) checks for systems with zero
trips/stops and kicks off an immediate fetch for just those — a safety net distinct from
the daily refresh, gated by the same durable "already fetched today" check so it doesn't
turn into a tight retry loop.

The daily-fetch gate (`TransitSystem.last_fetched_at`) is DB-backed specifically because
Celery worker containers get restarted often (deploys, crashes) and any purely
in-container state (a local marker file, an in-memory flag) would reset on every restart,
defeating the "once a day" intent. See `incidents-and-lessons.md` for the incident that
motivated this.

## API surface

- `GET /trip_updates/{transit_system}` — live positions, SSE.
- `GET /trip_detail/{transit_system}/{trip_id}` — full detail on one active trip (every
  remaining stop, vehicle info, route detail) — a deeper read than the SSE event carries.
- `GET /service_alerts/{transit_system}` — GTFS-RT ServiceAlerts, when a system has them.
- `GET /transit_systems` — names of active systems.
- `GET /transit_systems/{transit_system}` — slow-changing per-system metadata (timezone,
  next scheduled realtime update time for quota-grouped systems).
- `GET /info` — basic service info/health.

See the main `README.md` for exact response shapes and more detail on each endpoint's
fallback behavior.
