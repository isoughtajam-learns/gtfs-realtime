"""
Proactively keeps RealtimeFeedCache warm for every quota_group (see
SharedFeedQuota) on an even, predictable cadence, instead of relying
purely on inbound HTTP requests to trigger fetches. Demand-driven fetching
alone (RealtimeFeedCache.get()'s quota-aware path, still in place as a
recovery/bootstrap fallback - see below) has a real failure mode under
concurrent multi-system load: several systems each wanting a fresh fetch
every ~30s will burn through a shared 55/hour budget in minutes, then
serve increasingly stale data for the rest of the hour - a sharp cliff,
not a graceful slowdown. Confirmed by hand: 8 systems at a 30s interval
against a 55/hour budget exhausts it in about 3 minutes.

QuotaGroupScheduler instead spends exactly one token every
`3600 / budget` seconds, round-robining which group member gets refreshed
- so every member gets an equal, fully predictable turn (e.g. roughly
every ~11 minutes for an 8-member group at a 55/hour budget), and the
group's real request rate can never exceed the budget by construction
(the pacing IS the enforcement, with SharedFeedQuota.try_consume as a
backstop in case the pacing math is ever slightly off). Every 5th tick
services ServiceAlerts instead of TripUpdates (an ~80/20 split) - alerts
don't need anywhere near live-position freshness, so giving them the same
cadence would roughly halve TripUpdates' effective refresh rate for no
real benefit.

RealtimeFeedCache.get()'s own quota_group-aware demand-driven path (see
that module) stays in place unchanged as a recovery mechanism: a
system's very first request (nothing cached yet) still bypasses quota
entirely rather than returning nothing, and if this scheduler ever falls
behind on a system for longer than that system's own
min_poll_interval_seconds (set well above this scheduler's normal visit
cadence for quota_group members - see the migration that introduced
quota_group), a real HTTP request can still trigger a recovery fetch.
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from src.services.realtime_feed_cache import RealtimeFeedCache
from src.services.regional_feed import split_feed_by_agency
from src.services.shared_feed_quota import SharedFeedQuota

logger = logging.getLogger(__name__)

# Every 5th tick services ServiceAlerts instead of TripUpdates - see this
# module's docstring for why alerts get the sparser slice.
ALERTS_TICK_EVERY = 5


class QuotaGroupScheduler:
    def __init__(
        self,
        quota_group: str,
        members: List[Dict[str, Any]],
        fetch: Callable[[str], Awaitable[Any]],
        budget: int,
    ) -> None:
        """`budget` (real requests/hour for `quota_group`) is passed in
        explicitly rather than looked up from
        shared_feed_quota.QUOTA_GROUP_LIMITS internally - keeps this class
        decoupled from that global config/importable and constructible
        with any test-only group name, with the caller (see
        main.py's _start_quota_group_schedulers) responsible for actually
        reading the real budget."""
        self.quota_group = quota_group
        self.members = members
        self.fetch = fetch
        self.tick_interval_seconds = 3600 / budget
        self._alerts_members = [m for m in members if m.get("alerts_url")]
        self._trip_updates_cursor = 0
        self._alerts_cursor = 0
        self._tick_count = 0
        # Unix timestamps (seconds) - populated immediately for every
        # member's first scheduled turn, so GET /transit_systems/{id}
        # has a real value even before this scheduler's first tick has
        # actually fired. Only tracks TripUpdates turns - the field this
        # backs (TransitSystemDetail.next_realtime_update_at) is about
        # live positions, not alerts.
        self.next_update_at: Dict[str, float] = {}
        now = time.time()
        for index, member in enumerate(members):
            self.next_update_at[member["name"]] = (
                now + (index + 1) * self.tick_interval_seconds
            )

    async def tick(self) -> None:
        """One scheduled unit of work: either a TripUpdates or a
        ServiceAlerts refresh for whichever member is next in that duty's
        rotation. Wrapped by run() in a try/except per tick, so one bad
        poll can't kill the whole scheduler - same principle as
        transit_feed()'s own poll loop."""
        self._tick_count += 1
        if self._tick_count % ALERTS_TICK_EVERY == 0:
            await self._tick_alerts()
        else:
            await self._tick_trip_updates()

    async def _tick_trip_updates(self) -> None:
        if not self.members:
            return
        member = self.members[self._trip_updates_cursor]
        self._trip_updates_cursor = (self._trip_updates_cursor + 1) % len(self.members)
        await RealtimeFeedCache.get(
            member["name"],
            lambda: self.fetch(member["realtime_url"]),
            0,
            self.quota_group,
        )
        self.next_update_at[member["name"]] = (
            time.time() + len(self.members) * self.tick_interval_seconds
        )

    async def _tick_alerts(self) -> None:
        if not self._alerts_members:
            return
        member = self._alerts_members[self._alerts_cursor]
        self._alerts_cursor = (self._alerts_cursor + 1) % len(self._alerts_members)
        await RealtimeFeedCache.get(
            f"{member['name']}:alerts",
            lambda: self.fetch(member["alerts_url"]),
            0,
            self.quota_group,
        )

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as ex:
                logger.error(
                    f"Error in quota_group scheduler tick for {self.quota_group}: {ex}"
                )
            await asyncio.sleep(self.tick_interval_seconds)


class _HasNextUpdateAt(Protocol):
    next_update_at: Dict[str, float]


class RegionalFeedScheduler:
    """Replaces QuotaGroupScheduler's per-system round-robin for every
    511.org-backed system that's been cut over to the regional feed (see
    TransitSystem.realtime_feed_agency_code, src/services/regional_feed.py)
    - instead of round-robining one member's turn per tick (diluting the
    group's fixed budget across however many members there are, the core
    problem documented in docs/rate-limiting-and-scheduling.md), ONE fetch
    per tick covers every regional-fed member at once, demuxed by agency
    code. Confirmed correct for both TripUpdates and ServiceAlerts against
    live SF-MTA data before ever being wired up to real traffic - see
    IRL-15's findings.

    Writes straight into RealtimeFeedCache via .set() rather than going
    through .get() (unlike QuotaGroupScheduler) - there's no one URL per
    member to inject as a `fetch` callable here, since a single response
    covers every member at once."""

    def __init__(
        self,
        quota_group: str,
        agency_code_to_transit_system: Dict[str, str],
        trip_updates_url: str,
        alerts_url: str,
        fetch: Callable[[str], Awaitable[Any]],
        budget: int,
    ) -> None:
        self.quota_group = quota_group
        self.agency_code_to_transit_system = agency_code_to_transit_system
        self.trip_updates_url = trip_updates_url
        self.alerts_url = alerts_url
        self.fetch = fetch
        self.tick_interval_seconds = 3600 / budget
        self._tick_count = 0
        now = time.time()
        self.next_update_at: Dict[str, float] = {
            transit_system: now + self.tick_interval_seconds
            for transit_system in agency_code_to_transit_system.values()
        }

    async def tick(self) -> None:
        self._tick_count += 1
        if self._tick_count % ALERTS_TICK_EVERY == 0:
            await self._tick_alerts()
        else:
            await self._tick_trip_updates()

    async def _tick_trip_updates(self) -> None:
        if not self.agency_code_to_transit_system:
            return
        # No RealtimeFeedCache.get() to delegate quota accounting to here
        # (see this class's docstring) - check it directly, same backstop
        # SharedFeedQuota.try_consume provides everywhere else. Skipping
        # the tick on an exhausted budget leaves each member's existing
        # cached feed in place (however stale), same fallback as .get()'s.
        if not SharedFeedQuota.try_consume(self.quota_group):
            return
        feed = await self.fetch(self.trip_updates_url)
        demuxed = split_feed_by_agency(feed, self.agency_code_to_transit_system)
        now = time.time()
        for transit_system, message in demuxed.items():
            RealtimeFeedCache.set(transit_system, message)
            self.next_update_at[transit_system] = now + self.tick_interval_seconds

    async def _tick_alerts(self) -> None:
        if not self.agency_code_to_transit_system:
            return
        if not SharedFeedQuota.try_consume(self.quota_group):
            return
        feed = await self.fetch(self.alerts_url)
        demuxed = split_feed_by_agency(feed, self.agency_code_to_transit_system)
        for transit_system, message in demuxed.items():
            RealtimeFeedCache.set(f"{transit_system}:alerts", message)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as ex:
                logger.error(
                    f"Error in regional feed scheduler tick for {self.quota_group}: {ex}"
                )
            await asyncio.sleep(self.tick_interval_seconds)


def next_realtime_update_at(
    schedulers: List[_HasNextUpdateAt], transit_system: str
) -> Optional[int]:
    """Looked up by GET /transit_systems/{transit_system} - None for a
    system not in any scheduled quota_group (nothing proactively keeps it
    warm; it's fetched purely on demand, same as before this module
    existed)."""
    for scheduler in schedulers:
        if transit_system in scheduler.next_update_at:
            return int(scheduler.next_update_at[transit_system])
    return None
