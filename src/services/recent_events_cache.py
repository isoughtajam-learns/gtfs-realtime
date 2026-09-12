"""
Per-transit-system rolling cache of the 50 most-recently-updated live
trip_update events - see RecentEventsCache. transit_feed() (src/main.py)
yields this cache's contents as the very first thing sent to a newly
connected SSE client, so they get real data immediately instead of
waiting out this system's poll/rate-limit cycle (up to
TransitSystem.min_poll_interval_seconds - e.g. SF-MTA's 240s) for the
first live event.
"""

from typing import Dict, Iterable, List

from src.models import TripPosition

MAX_EVENTS = 50


class RecentEventsCache:
    """Keyed by transit_system, then by trip_id - one entry per trip (the
    most recent position seen for it), not a full event log. update()
    merges each poll's positions in and, once a system has more than
    MAX_EVENTS distinct trips cached, keeps only the MAX_EVENTS with the
    highest `timestamp` (GTFS-RT trip_update.timestamp, or the feed
    header's timestamp when a source doesn't set the per-trip one - see
    main.py's transit_feed()). A trip that stops appearing in the live
    feed isn't explicitly evicted - it just ages out once enough fresher
    trips have taken its place in the ranking."""

    _events: Dict[str, Dict[str, TripPosition]] = {}

    @classmethod
    def update(cls, transit_system: str, positions: Iterable[TripPosition]) -> None:
        system_cache = cls._events.setdefault(transit_system, {})
        for position in positions:
            system_cache[position.trip_id] = position
        if len(system_cache) > MAX_EVENTS:
            newest = sorted(
                system_cache.values(), key=lambda p: p.timestamp or 0, reverse=True
            )[:MAX_EVENTS]
            cls._events[transit_system] = {p.trip_id: p for p in newest}

    @classmethod
    def get(cls, transit_system: str) -> List[TripPosition]:
        """Oldest first - the order a continuously-connected client would
        have received these in, had they been connected the whole time."""
        return sorted(
            cls._events.get(transit_system, {}).values(),
            key=lambda p: p.timestamp or 0,
        )
