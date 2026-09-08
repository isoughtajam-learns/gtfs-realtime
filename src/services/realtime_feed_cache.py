"""
Coalesces and rate-limits outbound polls to a transit system's own
realtime API. Without this, main.py's transit_feed() (one poll per
connected SSE client, every 30s) and trip_detail() (one poll per request,
previously uncached entirely) could each independently exceed a strict
upstream limit - some sources enforce a per-key quota (e.g. 511.org) that
concurrent, uncoordinated polling could blow through. Every caller for the
same transit_system now shares one fetch, refreshed at most once every
TransitSystem.min_poll_interval_seconds (see
src/services/transit_system_detail.py) - a value of 0 (the default for
every system unless explicitly configured) means never cache, matching
pre-rate-limiting behavior exactly.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, TypeVar, cast

T = TypeVar("T")


class RealtimeFeedCache:
    # Typed Any, not gtfs_realtime_pb2.FeedMessage: get()'s TypeVar makes
    # each call fully type-checked against its own `fetch`, but the shared
    # class-level storage itself has to hold whatever type any caller cached.
    _feed: Dict[str, Any] = {}
    _fetched_at: Dict[str, datetime] = {}
    _locks: Dict[str, asyncio.Lock] = {}

    @classmethod
    async def get(
        cls,
        transit_system: str,
        fetch: Callable[[], Awaitable[T]],
        min_interval_seconds: int,
    ) -> T:
        """Returns the cached feed for `transit_system` if it's within
        `min_interval_seconds` old, otherwise awaits `fetch()` for a fresh
        one and caches it. `fetch` is injected rather than this module
        importing main.py's _fetch_feed directly - keeps this a service
        (main.py depends on services, not the other way around) and lets
        it be tested without mocking requests.get.

        A per-system asyncio.Lock, double-checked after acquiring (same
        pattern as ScheduleCache), means concurrent callers racing a stale
        cache share one real fetch instead of each making their own -
        without it, min_interval_seconds couldn't actually bound the
        outbound request rate under concurrent load (e.g. several SSE
        clients whose 30s poll cycles happen to line up).

        A `fetch()` exception propagates as-is and never gets cached, so a
        single failed poll can't poison subsequent callers or block a
        retry on the next one."""
        if not cls._is_fresh(transit_system, min_interval_seconds):
            lock = cls._locks.setdefault(transit_system, asyncio.Lock())
            async with lock:
                if not cls._is_fresh(transit_system, min_interval_seconds):
                    cls._feed[transit_system] = await fetch()
                    cls._fetched_at[transit_system] = datetime.utcnow()
        return cast(T, cls._feed[transit_system])

    @classmethod
    def _is_fresh(cls, transit_system: str, min_interval_seconds: int) -> bool:
        fetched_at = cls._fetched_at.get(transit_system)
        if fetched_at is None or transit_system not in cls._feed:
            return False
        return datetime.utcnow() - fetched_at < timedelta(seconds=min_interval_seconds)
