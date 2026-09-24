import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest

from src.services import realtime_feed_cache as cache_module
from src.services.realtime_feed_cache import RealtimeFeedCache
from src.services.shared_feed_quota import QUOTA_GROUP_LIMITS, SharedFeedQuota


@pytest.fixture(autouse=True)  # type: ignore[misc]
def _clear_shared_feed_quota() -> None:
    SharedFeedQuota._timestamps.clear()


class _FetchStub:
    """Async callable that yields each of `values` in order on successive
    calls (raising it instead, if a value is an Exception), and records how
    many times it was called."""

    def __init__(self, *values: Any) -> None:
        self._remaining = list(values)
        self.calls = 0

    async def __call__(self) -> Any:
        self.calls += 1
        value = self._remaining.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_fetches_fresh_every_time_when_min_interval_is_zero() -> None:
    # 0 is the default for every system unless explicitly rate-limited -
    # must behave exactly like the pre-caching code (always fetch fresh).
    fetch = _FetchStub("feed-1", "feed-2")

    result1 = asyncio.run(RealtimeFeedCache.get("zero-interval-system", fetch, 0))
    result2 = asyncio.run(RealtimeFeedCache.get("zero-interval-system", fetch, 0))

    assert result1 == "feed-1"
    assert result2 == "feed-2"
    assert fetch.calls == 2


def test_caches_within_the_min_interval() -> None:
    fetch = _FetchStub("feed-1", "feed-2")

    result1 = asyncio.run(RealtimeFeedCache.get("cached-system", fetch, 60))
    result2 = asyncio.run(RealtimeFeedCache.get("cached-system", fetch, 60))

    assert result1 == "feed-1"
    assert result2 == "feed-1"  # second call reused the cached result
    assert fetch.calls == 1


def test_refetches_once_the_interval_has_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = _FetchStub("feed-1", "feed-2")
    now = {"value": datetime(2026, 1, 1, 12, 0, 0)}

    class _FakeDatetime(datetime):
        @classmethod
        def utcnow(cls) -> datetime:  # type: ignore[override]
            return now["value"]

    monkeypatch.setattr(cache_module, "datetime", _FakeDatetime)

    result1 = asyncio.run(RealtimeFeedCache.get("expiring-system", fetch, 30))
    now["value"] += timedelta(seconds=31)
    result2 = asyncio.run(RealtimeFeedCache.get("expiring-system", fetch, 30))

    assert result1 == "feed-1"
    assert result2 == "feed-2"
    assert fetch.calls == 2


def test_different_transit_systems_cached_independently() -> None:
    fetch_a = _FetchStub("feed-a")
    fetch_b = _FetchStub("feed-b")

    result_a = asyncio.run(RealtimeFeedCache.get("system-a", fetch_a, 60))
    result_b = asyncio.run(RealtimeFeedCache.get("system-b", fetch_b, 60))

    assert result_a == "feed-a"
    assert result_b == "feed-b"


def test_a_failed_fetch_is_not_cached_and_can_be_retried() -> None:
    fetch = _FetchStub(RuntimeError("upstream down"), "feed-recovered")

    with pytest.raises(RuntimeError):
        asyncio.run(RealtimeFeedCache.get("flaky-system", fetch, 60))

    result = asyncio.run(RealtimeFeedCache.get("flaky-system", fetch, 60))

    assert result == "feed-recovered"
    assert fetch.calls == 2


def test_concurrent_callers_share_one_fetch_when_cache_is_stale() -> None:
    # Without the per-system lock, N concurrent callers hitting a stale
    # cache at once would each trigger their own fetch - min_interval_seconds
    # couldn't actually bound the outbound request rate under real
    # concurrent load (e.g. several SSE clients whose poll cycles line up).
    calls = {"n": 0}

    async def slow_fetch() -> str:
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return "feed"

    async def gather_concurrent_callers() -> list[str]:
        return await asyncio.gather(
            *[
                RealtimeFeedCache.get("concurrent-system", slow_fetch, 60)
                for _ in range(5)
            ]
        )

    results = asyncio.run(gather_concurrent_callers())

    assert results == ["feed"] * 5
    assert calls["n"] == 1


def test_peek_returns_none_when_nothing_cached_yet() -> None:
    assert RealtimeFeedCache.peek("never-fetched-system") is None


def test_peek_returns_cached_value_without_triggering_a_fetch() -> None:
    fetch = _FetchStub("feed-1")

    asyncio.run(RealtimeFeedCache.get("peeked-system", fetch, 9999))

    assert RealtimeFeedCache.peek("peeked-system") == "feed-1"
    assert fetch.calls == 1  # peek() itself made no additional call


def test_peek_ignores_min_interval_freshness() -> None:
    # peek() has no min_interval_seconds param at all - it returns whatever
    # is cached regardless of age, unlike get().
    fetch = _FetchStub("feed-1")
    asyncio.run(RealtimeFeedCache.get("stale-but-peekable-system", fetch, 0))

    assert RealtimeFeedCache.peek("stale-but-peekable-system") == "feed-1"


def test_quota_group_none_never_touches_shared_quota() -> None:
    # Default/unconfigured behavior (every system before quota_group
    # existed) - fetches happen unconditionally, exactly as before.
    fetch = _FetchStub("feed-1", "feed-2")

    result1 = asyncio.run(
        RealtimeFeedCache.get("no-group-system", fetch, 0, quota_group=None)
    )
    result2 = asyncio.run(
        RealtimeFeedCache.get("no-group-system", fetch, 0, quota_group=None)
    )

    assert result1 == "feed-1"
    assert result2 == "feed-2"
    assert fetch.calls == 2


def test_quota_group_fetches_while_budget_remains() -> None:
    fetch = _FetchStub("feed-1")

    result = asyncio.run(
        RealtimeFeedCache.get("quota-system-a", fetch, 0, quota_group="test-group-a")
    )

    assert result == "feed-1"
    assert fetch.calls == 1


def test_quota_group_serves_stale_cache_once_budget_is_exhausted() -> None:
    # First call populates the cache and spends the group's only token.
    # Once the group is out of budget, a due-for-refresh call must serve
    # the existing cached feed rather than making another real request -
    # that's the whole point (a budget shared with other transit_systems,
    # e.g. every 511.org-backed one on our one API key).
    QUOTA_GROUP_LIMITS["test-group-b"] = 1
    try:
        fetch = _FetchStub("feed-1", "feed-2")

        result1 = asyncio.run(
            RealtimeFeedCache.get(
                "quota-system-b", fetch, 0, quota_group="test-group-b"
            )
        )
        result2 = asyncio.run(
            RealtimeFeedCache.get(
                "quota-system-b", fetch, 0, quota_group="test-group-b"
            )
        )

        assert result1 == "feed-1"
        assert result2 == "feed-1"  # stale cache served, not feed-2
        assert fetch.calls == 1
    finally:
        del QUOTA_GROUP_LIMITS["test-group-b"]


def test_quota_group_still_fetches_when_nothing_cached_yet_despite_exhausted_budget() -> (
    None
):
    # A transit_system's very first request has no stale fallback to serve
    # - proceeding anyway (accepting a rare/minor over-budget spend) beats
    # returning nothing for a system that's never had any data at all.
    QUOTA_GROUP_LIMITS["test-group-c"] = 1
    try:
        # Exhaust the group's budget via an unrelated transit_system first.
        other_fetch = _FetchStub("other-feed")
        asyncio.run(
            RealtimeFeedCache.get(
                "quota-system-c-other", other_fetch, 0, quota_group="test-group-c"
            )
        )

        fetch = _FetchStub("feed-1")
        result = asyncio.run(
            RealtimeFeedCache.get(
                "quota-system-c-new", fetch, 0, quota_group="test-group-c"
            )
        )

        assert result == "feed-1"
        assert fetch.calls == 1
    finally:
        del QUOTA_GROUP_LIMITS["test-group-c"]
