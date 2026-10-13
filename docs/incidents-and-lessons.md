# Incidents and Lessons

Chronological, grouped by theme. Each entry has a root cause and a fix — read before
touching the same area, since several of these are easy to silently reintroduce.

## Fetcher / data-pipeline incidents

### Silent upsert failures masked as success (two separate occurrences)

**First occurrence**: `upsert_trips`/`upsert_stops`/`upsert_routes` each ended with a
`print(f"... {result.keys()}")` on a bulk multi-row `insert(...).values([...])` result —
calling `.keys()` on that kind of SQLAlchemy result raises `ResourceClosedError`. All
three ran inside one `try/except`, so the crash on `upsert_trips`'s own print statement
silently aborted `upsert_stops`/`upsert_routes` for **every** transit system, every time.
`trip` had rows; `stop`/`route` never did. Fixed by switching to `result.rowcount`.

**Second occurrence, worse than first time understood**: a later fix for a
`CardinalityViolation` (duplicate `ON CONFLICT` keys within one batch — see below)
silently regressed during an unrelated file reorganization, since nothing tested for it.
When it recurred, the actual damage was worse than the original incident's own
post-mortem concluded: `do_upserts()` runs every upsert inside *one* transaction with a
`try/except` *inside* the `with engine.begin()` block. The Python exception gets caught
(so the loop moves on and logs row counts as if everything succeeded), but Postgres had
already aborted the whole underlying transaction — so the implicit commit at the end of
the `with` block silently no-ops, discarding *everything* from that fetch, not just the
table that errored. A system's schedule data was frozen on weeks-old data with every
scheduled fetch failing identically, and no visible error anywhere except a backend log
line nobody was watching.

**The lesson, stated generally**: a fetcher fix "verified" only by watching console
output (row counts, "upserts completed" messages) is not actually verified, because
printed success can coexist with a fully rolled-back transaction. Confirm persistence
via the database directly — a timestamp column that only a successful upsert sets, or
the actual row counts — before trusting a fetcher fix or any report that one already
happened. The dedup logic that caused the regression was eventually pulled into a pure,
unit-tested function specifically so it can't silently vanish in a refactor again
without a test failing.

### `CardinalityViolation` on `ON CONFLICT DO UPDATE`

Postgres can't apply `ON CONFLICT DO UPDATE` twice to the same row within one SQL
statement. A batched upsert helper crashed when two source rows sharing a conflict key
landed in the same batch — surfaced for real with a source whose `routes.txt` reused the
same `route_short_name` (a *global*, not per-system, unique column) across dozens of
distinct `route_id`s for shuttle-bus-replacement routes. Fixed generally, not for that
one source: deduplicate the full row list by the actual conflict columns (keeping the
last occurrence — normal upsert "last write wins" semantics) *before* chunking into
batches, rather than hoping no single batch happens to contain a clash.

### Large-feed memory exhaustion on a small instance

A source with an unusually large `stop_times.txt` (GTFS's biggest file — one row per
stop visit per trip) caused memory exhaustion on first ingestion. Root cause was
architectural: `upsert_trips` and `upsert_stops` each independently parsed the *entire*
file from scratch. Fixed by merging into a single streaming pass whose results get
passed into both upsert methods, plus batching every upsert regardless of table size.
Measured ~4.8× peak memory reduction on the same real dataset, confirmed via
`resource.getrusage().ru_maxrss` before/after, and confirmed flat memory in production
via `free -h`.

A second, compounding cause: the backend's container boot command called the fetcher
with `force=True`, which bypasses the "already fetched today" freshness gate entirely —
meaning the full memory-expensive parse ran on **every container restart or redeploy**,
not once a day as the scheduling design assumed. Fixed by removing the fetcher call from
the boot command; schedule data now comes only from the periodic Celery task, which
correctly respects the once-daily gate.

### The once-daily fetch gate wasn't actually once-daily

The original freshness check lived in a local marker file inside the celery-worker
container's filesystem — which gets wiped on every restart/redeploy. Since that
container restarts often (deploys, crashes), the "once per day" gate reset constantly in
practice. Fixed by moving the gate to a DB column (`TransitSystem.last_fetched_at`),
checked before the fetch attempt — durable across restarts because it lives in the
database, not any one container's disk.

### Celery task silently never ran

A periodic task was registered in `beat_schedule` under a name that didn't match the
task's *actual* registered name (Celery module-qualifies task names based on how the
worker is invoked — `celery -A src.tasks worker` registers `src.tasks.foo`, not bare
`tasks.foo`). A dispatched task under an unregistered name is silently dropped by the
worker — no error, just never runs. Caught only by noticing stale data far past the
expected refresh window. **Always verify a task's actual registered name via the
worker's own startup `[tasks]` log banner before wiring it into a schedule.**

### Optional-GTFS-column handling: three related bugs from one new source

All in the same class — "GTFS spec says this column is optional, but the code assumed
it was always either present-and-valid or entirely absent, not present-but-empty":

- A date field's fallback-default logic only triggered when the *column* was missing,
  not when the column existed but was empty — crashed parsing an empty string as a date.
- A required-looking field actually defaults per the GTFS spec when its column is
  absent entirely — code required an explicit value, silently skipping every row for any
  source that omits the column (which the spec explicitly allows).
- A field required "any", but it's genuinely optional in the GTFS spec and a legitimate
  source had no values for it at all — every row failed a completeness check as a
  result, meaning a new source could never actually pass ingestion even after the other
  two fixes. Fixed by dropping it from the required set (kept as an informational,
  non-blocking count in the diagnosis output), following the precedent set by an earlier
  source with missing route colors.

**The general lesson**: "optional in the GTFS spec" has at least three distinct shapes
in real feed data — column absent, column present but blank, column present with a
valid default — and code written to handle only one or two of these will break on a
new source that hits the uncovered case. The diagnosis-before-ingest tooling
(`fetcher.py --diagnose`) exists specifically to surface these gaps before they become a
silent data loss in production.

## Security incident: hardcoded API key on a public repo

A migration (part of onboarding a new Bay Area transit system) hardcoded a 511.org API
key as a literal string in the migration file, following the exact pattern of an
*already-merged* migration from an earlier onboarding — a pattern that, unknown at the
time, was already present in **two** previously-merged migrations. The repo is public on
GitHub. The in-progress PR got a credential-scanning warning and was deleted — but by
then the key had already been exposed in git history via the two earlier merges, not
just the deleted PR.

**Resolution, in order**:
1. The key itself was rotated by its owner directly with the provider (not something an
   AI assistant can do — it's an external developer-portal action with no API access).
2. Going forward, no secret is ever written as a literal in any committed file,
   migration or otherwise. New secrets go in AWS Secrets Manager and get read via
   `get_settings().<field>` at runtime/migration-time. See `deployment.md`'s Secrets
   section and `conventions.md`.
3. A dedicated migration was written to re-point every affected URL at the new key,
   reading it live from the environment rather than a literal — and it deliberately
   **raises** if the expected environment variable isn't set, rather than silently
   leaving the old (compromised) key in place or writing an unauthenticated URL.
   Already-merged migration files that contained the old key were left as-is (rewriting
   merged migration history is its own risk, and the old key has to be treated as
   compromised regardless of what those files literally say).

### The bootstrapping gap this created in the deploy pipeline

The automated deploy pipeline runs database migrations *before* `terraform apply` — on
purpose, so a bad migration fails the deploy cleanly before the live service ever points
at new infrastructure. But the migration step works by cloning the **currently
registered** ECS task definition (from *before* this deploy) and swapping in the new
image. The very first deploy that both (a) introduces a brand-new secret in Terraform
*and* (b) needs that secret during the migration itself hits a chicken-and-egg problem:
the task definition the migration runs under doesn't have the new secret wired in yet,
because only `terraform apply` — which runs *after* the migration step — would add it.

Confirmed directly: the key-rotation migration's deploy failed with the exact
`RuntimeError` it was written to raise on a missing environment variable, because the
cloned migration task genuinely had no `API_KEY_511_ORG` set. Production itself was
unaffected — the migration-gate design did exactly its job and aborted before
`terraform apply` ever touched the live service.

**Fix, and the general pattern for any future "brand-new secret the migration itself
needs" situation**: bootstrap out of band via the manual deploy script
(`deployment/deploy.sh`), which does **not** run migrations at all — it just builds,
pushes, and applies Terraform directly. One such run registers a task definition that
finally includes the new secret, with no migration risk (since none runs). This is safe
specifically because the new code didn't *require* the pending migration to function —
the old, not-yet-rotated value was still valid. Once that bootstrap run completes, the
*next* normal automated deploy's migration step clones a task definition that already
has the secret, and succeeds through the normal pipeline from then on.

## Infrastructure process mistake: wrong tool destination

Asked to migrate GitHub Issues to a separate issue tracker, the migration initially used
the **wrong workspace** — an account-level default connection that happened to already
be available, rather than the project-specific one just configured for this purpose.
The mistake wasn't caught until after issues had already been created and the
originals closed with cross-reference links.

**Root cause**: two separate, same-vendor integrations were available at once (an
always-on default connection, and a newly-added project-scoped one with its own
credentials) with very similar-looking tool names. The newly-added one wasn't actually
connected yet in the running session — a config file containing its setup had a subtle
encoding bug (see next entry) that made it fail to parse — but a same-vendor tool from
the other, already-working connection was available and used without checking that it
was pointed at the intended destination.

**Fix and general lesson**: when more than one connection to the same kind of external
service might be available, confirm which workspace/account a tool is actually
connected to (most such integrations expose a "what workspace is this" check) *before*
creating anything, especially before any action that's hard to undo cleanly (here:
closing the original issues). Cleanup was possible (the wrong destination's items were
marked canceled/retired with an explanatory note, rather than silently deleted, and the
original issues' cross-reference links were corrected once the mistake was found) but is
pure rework that a single up-front check would have avoided.

### The underlying cause: non-breaking spaces in a config file

The newly-configured connection's setup file failed to parse as valid JSON, even though
it displayed as completely normal, correctly-formatted JSON in every tool used to view
it. Root cause: every indentation "space" in the file was actually a **non-breaking
space** (Unicode U+00A0), not a regular ASCII space — invisible in a terminal or editor,
but not valid JSON whitespace per the JSON spec. This is a classic artifact of
copy-pasting text out of a rendered web page or chat UI (which often substitutes regular
spaces with non-breaking ones for display) into a plain-text editor, rather than typing
or pasting genuinely plain text.

**Lesson**: if a config file "looks obviously correct" by eye but a strict parser
rejects it outright, check for invisible/non-standard whitespace characters before
assuming the parser is wrong or the content is corrupted some other way — a byte-level
dump (not just viewing the file normally) is the fast way to confirm this.

## Infrastructure drift: unplanned EC2 instance replacement

The production EC2 instance was replaced mid-session without anyone intentionally
touching EC2-related Terraform resources. Root cause: the instance's AMI was looked up
via AWS's "latest recommended" SSM parameter rather than a pinned value — *any*
`terraform apply`, even for completely unrelated resources, can silently force-replace
the instance if that AMI drifted since the last apply. This causes a new SSH host key
(expected, not itself a security problem, but worth verifying via `describe-instances`
rather than assuming something is wrong). See `deployment.md` — not fixed as of this
writing; pinning the AMI would prevent this at the cost of needing a deliberate step to
pick up real AMI updates.
