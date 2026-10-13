# Project Docs

This folder captures project history, architectural rationale, and lessons learned that
aren't in the main `README.md` (which documents the current API/setup) or in git history
(which shows *what* changed but rarely *why*). It's written to be portable across any AI
coding assistant or human engineer picking up this project — no tool-specific references.

**IRL Transit** (irltransit.com) is a live Bay Area transit tracker. This repo
(`gtfs-realtime`) is the backend: a FastAPI service that ingests GTFS-Realtime (live
vehicle positions, service alerts) and GTFS Schedule (static route/stop/trip data) feeds
from multiple transit agencies, and serves them over a REST/SSE API. A companion repo,
`gtfs-dashboard`, is the React frontend.

## Files

- [`architecture.md`](architecture.md) — system design: the FastAPI app, caching layers,
  the GTFS-RT ingestion pipeline, and how they fit together.
- [`rate-limiting-and-scheduling.md`](rate-limiting-and-scheduling.md) — why some sources
  need request-budget coordination across multiple systems, and the two-layer design
  (`SharedFeedQuota` + `QuotaGroupScheduler`) that handles it.
- [`transit-systems.md`](transit-systems.md) — the transit-system registry model, which
  systems are onboarded, per-system quirks and data gaps discovered along the way, and
  the Bay Area expansion plan.
- [`deployment.md`](deployment.md) — the three distinct ways this app runs (local, local
  Docker, AWS production), the production Terraform/ECS design, and the deploy pipeline.
- [`incidents-and-lessons.md`](incidents-and-lessons.md) — notable bugs, outages, and
  process mistakes, with root causes and fixes. Read this before touching the fetcher,
  secrets handling, or the deploy pipeline — several of these are easy to reintroduce.
- [`conventions.md`](conventions.md) — working conventions this project has settled on
  (migration sequencing, secret handling, versioning/deploy gating, PR scope) that aren't
  enforced by tooling and are easy to violate by accident.

## Quick orientation

- The transit-system registry is a database table (`TransitSystem`, via SQLAlchemy models
  in `src/models.py`), not a hardcoded config file. Each row names a system and its feed
  URLs; onboarding a new system means writing an Alembic migration that inserts a row.
- Live position data flows as Server-Sent Events from `GET /trip_updates/{system}`
  (`src/main.py`'s `transit_feed()`). Static schedule data (stop names, route colors,
  headsigns) is preloaded into an in-memory cache (`src/services/schedule_cache.py`) and
  used to enrich each live event without a DB round-trip per entity.
- Not every upstream feed is well-behaved — missing columns, ID-namespace mismatches
  between a system's realtime and schedule feeds, duplicate unique keys, and shared
  per-key rate limits have all shown up in production. See `incidents-and-lessons.md`.
