# Transit Systems

## The registry model

Transit systems are rows in a database table (`TransitSystem`, `src/models.py`), not
entries in a config file. This replaced an earlier design where active systems were
hardcoded in `src/constants.py` (`GTFS_URLS`/`GTFS_METADATA` dicts) — that file no longer
exists. Onboarding a new system means writing an Alembic migration that inserts a row
with its name, realtime/alerts/schedule URLs, and settings (`active`,
`min_poll_interval_seconds`, `quota_group`, `auth_required`).

**A migration that inserts a URL embedding an API key must never write that key as a
literal string in the file.** Read it live via `src.settings.get_settings()` instead.
See `incidents-and-lessons.md` for exactly what went wrong the one time this rule was
violated, and `conventions.md` for the resulting process.

## Currently onboarded (check `TransitSystem.active` for the live, authoritative list —
this is a snapshot, not guaranteed current)

- **BART** (Bay Area Rapid Transit)
- **SF-MTA** (SF Muni) — `quota_group = "511.org"`, `min_poll_interval_seconds = 900`,
  `auth_required = true`. The first (and so far only) system onboarded through 511.org.
- **MBTA** (Boston)
- **NY_Waterway**
- **Provence-Alpes** (France)

## Removed systems and why

Several systems were onboarded and later removed after investigation, rather than left
half-working:

- **Helsinki Regional Transport** — re-enabled once after a memory exhaustion incident
  was fixed (see `incidents-and-lessons.md`), but removed again later. Its `routes.txt`
  has no color columns at all (not just blank — absent), so its trips never got
  `color`/`text_color` even when active; this was a known, accepted gap, not what led to
  removal.
- **Estonia** — every `stop_time_update` in its live feed only set `arrival.delay`
  (relative), never absolute `arrival.time`/`departure.time`. The position-resolution
  logic only reads absolute times, so nothing ever got yielded. A real fix would mean
  merging the trip's *scheduled* time (from Schedule data) with the live `delay` to
  derive an absolute time — real feature work. Removed rather than pursued, partly over
  separate reliability concerns beyond just this gap.
- **Lisbon** — its registered realtime URL was returning the provider's own maintenance
  page (confirmed via response body, not actually a broken URL). Removed rather than
  left broken; may be worth re-adding if the outage was ever resolved.
- **Kiev** — onboarded, diagnosed, fixed several ingestion bugs for it (see
  `incidents-and-lessons.md`), later removed without a specifically recorded reason.

## Known per-system data gaps (not bugs — upstream data limitations)

- **BART**: the live feed's `TripDescriptor.route_id` is never populated, even though
  the trip's route is known from Schedule data. Every route-level lookup in this codebase
  (headsign, color, short/long name) has a trip-level fallback specifically to route
  around this — see `architecture.md`.
- **MBTA**: the realtime feed and static Schedule feed appear to use *different*
  stop/trip ID namespaces (e.g. realtime `stop_id: "1514"` vs. schedule `"70276"`).
  Lookups by ID come up empty, so MBTA events have `stop_id`/`trip_id`/`vehicle` from the
  live feed but `stop_name`/`trip_headsign` stay null even with correct Schedule data
  loaded. Not investigated further as of this writing — would need a cross-reference
  table or a different join key to fix.
- **Helsinki** (when it was active): no route color columns in `routes.txt` at all.
- Any system whose `routes.txt` is missing color columns will have a permanent 0
  "usable routes" count in `fetcher.py --diagnose` — this is treated as informational,
  not blocking (see `conventions.md`'s diagnosis-gate note), following the precedent set
  by Helsinki.

## The Bay Area expansion plan ("Phase 3")

The original plan, paced deliberately slowly by 511.org's shared rate limit (see
`rate-limiting-and-scheduling.md`): onboard SF-MTA first (done), then Caltrain, VTA, AC
Transit, Golden Gate Transit, SamTrans, SF Bay Ferry, and County Connection — "the Big 5
plus SF Ferry and County Connection" — one system per PR, each PR branched fresh off the
latest merged `main` rather than stacked on an unmerged sibling (migration files form a
single linear Alembic chain with no branch labels; stacking would risk two migrations
claiming the same `down_revision`).

**Caltrain's onboarding was started and abandoned mid-flight** (a stray local/remote git
branch, `add-caltrain`, still exists as of this writing — it was superseded by the
security fix below, never actually re-attempted). The attempt is what surfaced the API
key incident described in `incidents-and-lessons.md`: the migration hardcoded the
511.org key as a literal, following a pattern that (unknown at the time) was already
present in two *already-merged* migrations, on a public repo. Caltrain's onboarding
needs to be redone from scratch, reading the key via `get_settings().api_key_511_org`
rather than reusing anything from that branch.

**Update**: it's since been confirmed (see `rate-limiting-and-scheduling.md`'s last
section) that 511.org's API returns *all* Bay Area agencies — including every "phase 3"
system — in a single request via `agency=RG`, rather than one request per agency. This
removes the rate-limit constraint that made this expansion need to go one system at a
time in the first place; the plan as originally scoped (one onboarding PR per system,
each slower than the last as the quota group grows) is likely obsolete once the
`RG`-based fetch design lands, though that redesign itself hasn't been implemented yet.
