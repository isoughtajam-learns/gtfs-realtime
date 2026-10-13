# Rate Limiting and Scheduling

## The problem

511.org (the Bay Area's open transit data API) enforces a hard rate limit: **60 requests
per hour, shared across every agency parameter and every endpoint (TripUpdates,
ServiceAlerts) on one API key** — confirmed via that API's own `RateLimit-Limit`/
`RateLimit-Remaining` response headers, not just documentation. This is a *per-key*
budget, not per-agency: onboarding more Bay Area systems under the same key doesn't add
more quota, it just divides the existing 60/hour further.

This matters because the app's default polling model — each connected SSE client
independently polls its system every 30s, deduplicated only by `RealtimeFeedCache`'s
cache window — has no way to know that five *different* systems are all drawing on one
shared budget. Polling SF-MTA, BART-via-511, and Caltran all at their own independent
cadence could blow through the shared 60/hour limit even if each one individually looks
reasonable.

## The design: two cooperating layers

### `SharedFeedQuota` (`src/services/shared_feed_quota.py`)

A rolling-hour counter per named `quota_group` (e.g. `"511.org"`). `QUOTA_GROUP_LIMITS`
sets a self-imposed cap a bit under the real limit (55, not 60) for headroom.
`try_consume(quota_group)` is the gate: callers check it before spending a real request.

`RealtimeFeedCache.get()` takes an optional `quota_group` — when the budget for that
group is exhausted, it serves stale cached data instead of fetching, *except* for a
system's very first-ever request (no stale fallback exists yet, so that one bypasses the
quota rather than returning nothing).

### `QuotaGroupScheduler` (`src/services/quota_group_scheduler.py`)

Rather than leaving quota-grouped systems to compete for the shared budget reactively
(whoever polls first wins), this proactively *spends* the budget on a schedule: one
background task per active `quota_group`, started at app startup (`main.py`'s
`lifespan()`), round-robining exactly one real fetch per tick across every system in the
group. Tick interval is `3600 / budget` seconds (~65 seconds at a budget of 55). Every
5th tick services ServiceAlerts instead of TripUpdates (an ~80/20 TripUpdates/Alerts
split) — both draw on the same counter.

**Consequence of this design, worth remembering**: a system's own effective refresh
cadence is `N × tick_interval` where `N` is the number of systems in its quota group
(further diluted ×5/4 by the alerts split). Adding a system to the group doesn't change
the group's total budget — it divides the existing budget's attention across one more
system, meaning *everyone's* cadence gets slower. The tick interval itself
(`3600/budget`) stays fixed; only the per-system cadence changes as the roster changes.

The scheduler reads its member list from the database **once, at startup** — adding a
new system to the quota group requires a process restart to actually join the rotation
(which happens naturally via the normal migrate+deploy cycle, so this has never required
a special step in practice, just something to know if a system doesn't seem to be getting
polled after a migration-only change with no deploy).

### `next_realtime_update_at`

A field on `GET /transit_systems/{system}`'s response: a unix timestamp predicting the
scheduler's next real fetch for that system (`null` if the system isn't in any scheduled
quota group). Meant to back a frontend countdown UI, so users aren't left wondering why
data isn't updating as often as some other, non-quota-limited system.

## Why onboarding Bay Area systems was deliberately slow

The 511.org constraint directly shaped the pace of onboarding more Bay Area systems
(Caltrain, VTA, AC Transit, Golden Gate Transit, SamTrans, SF Bay Ferry, County
Connection were the planned "phase 3" set, one system onboarded at a time via its own
PR). Every additional system under the same 511.org key means a slower cadence for
*every* system already in the group — there was no way around this without either (a) a
higher quota from 511.org directly, or (b) finding a way to get more agencies' data per
request. A quota-increase request document was drafted summarizing the architecture
above specifically to make the case to 511.org's developer program for a higher limit.

## Confirmed: a single request covers every Bay Area agency

511.org's API takes an `agency` parameter — this app currently uses `"SF"` to get
SF-MTA only. **Verified directly against the live API**: `agency=RG` (region-wide)
returns all Bay Area agencies' data in one combined response, not one agency per
request.

What was checked, concretely:
- A direct `agency=SF` TripUpdates request and an `agency=RG` request, same moment:
  `RG`'s response is ~1.9× the byte size of `SF` alone, and decodes to a `FeedMessage`
  with far more entities (1931 vs. 901) and 238 distinct `route_id`s spanning **23**
  distinct agency-code prefixes (`SF`, `AC` [AC Transit], `SC` [likely VTA], `SM`
  [SamTrans], `BA` [BART], `CC` [County Connection], `GG` [Golden Gate Transit], `CT`
  [Caltrain], and more) — i.e. every planned "phase 3" system, plus several not even on
  the original plan, already present in one response.
- Entity ID format inside `RG` is `<AGENCY-CODE>:<original-id>` for both `route_id` and
  `trip_id` (e.g. `SF:12133132_M11`) — stripping the `<AGENCY-CODE>:` prefix gives back
  **exactly** the same ID format already used by this app's existing per-agency
  ingestion and stored Schedule data (confirmed by cross-checking stripped `RG`-mode
  SF trip_ids against real rows already in the `Trip` table). So demultiplexing is
  mechanically simple: split each entity's IDs on the first `:`, the prefix says which
  `TransitSystem` row it belongs to, the remainder is the same ID Schedule-matching
  already expects.
- The `RateLimit-Remaining` response header is **not reliable as a fine-grained signal**
  — observed non-monotonic even across consecutive calls to the *same* `agency=SF`
  value (did not decrease by exactly 1 each time, and in one sequence briefly
  increased). Likely multiple backend nodes each tracking a local, not perfectly
  synchronized count. Don't trust it for precise request-by-request accounting; the
  documented 60/hour limit itself wasn't contradicted, just the header's moment-to-moment
  reliability.

**Implication**: this removes the core reason Bay Area onboarding was paced one system
per PR. Rather than `QuotaGroupScheduler` round-robining one real fetch per system per
tick, every 511.org-backed system — current and all of "phase 3" — could be served from
one shared `agency=RG` fetch per tick, demultiplexed locally by the ID-prefix mapping
above. That's a meaningfully different design from the round-robin scheduler (one feed
fetch serving every quota-grouped system at once, rather than one system's turn per
tick) and worth designing deliberately — e.g. deciding how the demultiplexed entities
get attributed to each `TransitSystem` row's existing SSE-serving code, and whether
`QuotaGroupScheduler`'s per-system round-robin is still needed for anything once this
lands — rather than bolted on incrementally. Not yet implemented as of this writing.
