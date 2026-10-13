# Working Conventions

These aren't enforced by tooling (mostly), and several exist specifically because
violating them once already caused a real incident — see `incidents-and-lessons.md` for
the stories behind the ones that need it.

## Secrets

**Never write a secret as a literal string in any committed file — migrations
included.** Read it live via `src.settings.get_settings()` (a `pydantic-settings`
`Settings` object whose real-secret fields are `Optional[str]`, aliased to real
environment variable names, and deliberately absent from every committed `.env.*`
file). Locally, export the real value in your own shell or an uncommitted `.env`; in
production, it comes from AWS Secrets Manager, injected as a real ECS task-definition
environment variable. This repo is public on GitHub — there is no "private enough to get
away with it" tier for a hardcoded key here.

If a migration needs to write a URL or value containing a secret, it should read the
secret from settings and, ideally, **fail loudly** (raise) if the expected value isn't
set, rather than silently writing something broken or insecure.

## Migrations: single linear chain, no stacking unmerged branches

This repo keeps one linear Alembic history — no branch labels, no multi-head merges.
Each new migration should be written **after** the previous one has actually merged to
`main`, branching fresh each time, rather than stacking a new migration on top of an
unmerged sibling branch. Onboarding several systems "in parallel" means several
sequential PRs, not several simultaneously-open branches each adding a migration off the
same unmerged base.

## Deploy gating via `VERSION`

The automated deploy pipeline computes its release tag from the `VERSION` file and skips
deploying if that exact tag already exists. This is the mechanism for merging a PR
*without* triggering a deploy: simply don't bump `VERSION` in that PR. Use this
deliberately for any PR that would make `terraform apply` or a migration fail outright
if deployed immediately (e.g. referencing a secret that doesn't exist yet, a
genuinely destructive schema change) — merge it with `VERSION` unchanged, satisfy the
external precondition, then bump `VERSION` in a small, separate follow-up PR once it's
actually safe to deploy.

## PR scope

One logical change per PR, generally — but a small, genuinely low-risk fix (a stale
comment, a one-line safety fix like adding a sensitive file to `.gitignore`) found
*while already working in a file* is reasonable to fold into the PR already touching
that file, rather than always spinning up a separate PR for one line. Judgment call: if
the fix is unrelated enough that it would surprise a reviewer, or risky enough that it
deserves its own review attention, give it its own PR instead.

A PR's description should distinguish test changes, dev-process changes
(CI/lint/deploy config, docs), and actual functional changes — useful for a reviewer
skimming, and for anyone reading PR history later trying to understand what actually
shipped versus what was just process.

## Diagnosis before ingestion

New transit-system onboarding should run the fetcher's diagnosis mode
(`fetcher.py --diagnose --transit-system <name> --schedule-url <url>`) before writing
the onboarding migration, not after. The gate for "is this source usable" only requires
`stops["usable_rows"] > 0` — a missing route-color column or incomplete headsign data is
informational, not blocking, following the precedent set by sources that are
legitimately missing optional GTFS columns (see `transit-systems.md`).

## Verifying a fix actually worked

Watching console output (printed row counts, "success" log lines) is not sufficient
verification for anything touching the database inside a transaction — see
`incidents-and-lessons.md`'s upsert-transaction entry for exactly why printed success
can coexist with a fully rolled-back operation. Confirm via a direct database read
(a timestamp column, actual row counts, re-querying the specific thing that was
supposed to change) before considering a data-pipeline fix verified.

Similarly, for anything behind a live SSE/polling loop, prefer checking against the
*real* upstream feed and real stored data over a purely synthetic test, at least once,
before shipping — several real bugs here (ID-namespace mismatches between a feed's
realtime and schedule data, missing optional columns) were only visible against real
data, not any synthetic fixture.

## When onboarding reveals a gap in a shared mechanism

Several fixes in this codebase (batched-upsert deduplication, optional-column handling,
destination-headsign fallback chains) were explicitly written as **general** fixes
applicable to any source, not special-cased to the one system that happened to surface
the gap — even when only one system was affected at the time. The reasoning: the next
new source is likely to hit the same class of gap, and a special case silently fails to
cover it.
