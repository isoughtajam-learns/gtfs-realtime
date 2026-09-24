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
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.services.realtime_feed_cache import RealtimeFeedCache

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


def next_realtime_update_at(
    schedulers: List[QuotaGroupScheduler], transit_system: str
) -> Optional[int]:
    """Looked up by GET /transit_systems/{transit_system} - None for a
    system not in any scheduled quota_group (nothing proactively keeps it
    warm; it's fetched purely on demand, same as before this module
    existed)."""
    for scheduler in schedulers:
        if transit_system in scheduler.next_update_at:
            return int(scheduler.next_update_at[transit_system])
    return None
