"""
Coordinates outbound real-fetch budget shared across every transit_system
whose feeds are served by the same underlying rate-limited source. Confirmed
live: every 511.org-backed system (SF-MTA, and Bay Area systems onboarded
since) draws from ONE 60-requests/hour budget tied to our single API key -
its own RateLimit-Limit/RateLimit-Remaining response headers confirm the
"Remaining" count drops identically no matter which agency param or
endpoint (TripUpdates vs ServiceAlerts) is hit with that key. That's a
different kind of limit than RealtimeFeedCache.min_poll_interval_seconds
already handles: that throttles one *individual* transit_system's own feed
in isolation, with no way to know about a cap shared across many otherwise-
unrelated transit_system cache entries. This module is that missing piece -
a plain rolling-hour request counter per named quota_group, consulted by
RealtimeFeedCache before it actually calls a group member's fetch().
"""

import time
from collections import deque
from typing import Deque, Dict

# Deliberately below 511.org's real 60/hour limit (confirmed live via that
# API's own RateLimit-Limit response header) - leaves headroom for a
# same-hour burst (e.g. several group members cold-starting at once, each
# spending one token with nothing yet cached to fall back on - see
# RealtimeFeedCache.get) without actually tripping the source's own cap.
QUOTA_GROUP_LIMITS: Dict[str, int] = {
    "511.org": 55,
}

WINDOW_SECONDS = 3600


class SharedFeedQuota:
    _timestamps: Dict[str, Deque[float]] = {}

    @classmethod
    def try_consume(cls, quota_group: str) -> bool:
        """True (and records a spend) if `quota_group` has room in the
        current rolling hour; False (no spend recorded) otherwise. An
        unrecognized quota_group - not in QUOTA_GROUP_LIMITS - always
        returns True, treated as unbounded: only a name deliberately wired
        up here should ever actually constrain anything, so a typo or a
        not-yet-configured group fails open rather than silently blocking
        every fetch for it."""
        limit = QUOTA_GROUP_LIMITS.get(quota_group)
        if limit is None:
            return True
        now = time.monotonic()
        timestamps = cls._timestamps.setdefault(quota_group, deque())
        while timestamps and now - timestamps[0] >= WINDOW_SECONDS:
            timestamps.popleft()
        if len(timestamps) >= limit:
            return False
        timestamps.append(now)
        return True
