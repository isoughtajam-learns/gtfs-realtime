import asyncio
from typing import Any, List

import pytest

from src.services.quota_group_scheduler import (
    ALERTS_TICK_EVERY,
    QuotaGroupScheduler,
    next_realtime_update_at,
)
from src.services.realtime_feed_cache import RealtimeFeedCache
from src.services.shared_feed_quota import SharedFeedQuota


@pytest.fixture(autouse=True)  # type: ignore[misc]
def _clear_shared_state() -> None:
    RealtimeFeedCache._feed.clear()
    RealtimeFeedCache._fetched_at.clear()
    SharedFeedQuota._timestamps.clear()


class _FetchRecorder:
    """Async callable recording every URL it was asked to fetch, in order -
    stands in for the real _fetch_feed(url) injected into a scheduler."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    async def __call__(self, url: str) -> str:
        self.calls.append(url)
        return f"feed-for-{url}"


_TEST_BUDGET = 3600  # 1-second tick interval - fast for tests


def _members(*names: str) -> List[dict[str, Any]]:
    return [
        {
            "name": name,
            "realtime_url": f"http://example.com/{name}/tu",
            "alerts_url": f"http://example.com/{name}/alerts",
        }
        for name in names
    ]


def test_next_update_at_populated_for_every_member_at_construction() -> None:
    scheduler = QuotaGroupScheduler(
        "test-group-a", _members("A", "B", "C"), _FetchRecorder(), _TEST_BUDGET
    )

    assert set(scheduler.next_update_at.keys()) == {"A", "B", "C"}
    # Each member's initial slot is staggered - member k's first turn is
    # (k+1) ticks out, not all bunched at the same instant.
    times = [scheduler.next_update_at[name] for name in ("A", "B", "C")]
    assert times == sorted(times)
    assert len(set(times)) == 3


def test_trip_updates_ticks_round_robin_through_members() -> None:
    fetch = _FetchRecorder()
    scheduler = QuotaGroupScheduler(
        "test-group-b", _members("A", "B"), fetch, _TEST_BUDGET
    )

    # ALERTS_TICK_EVERY=5, so ticks 1-4 are trip_updates, tick 5 is alerts -
    # exercise just the trip_updates ticks here.
    asyncio.run(scheduler.tick())  # tick 1: A
    asyncio.run(scheduler.tick())  # tick 2: B
    asyncio.run(scheduler.tick())  # tick 3: A (wrapped around)

    assert fetch.calls == [
        "http://example.com/A/tu",
        "http://example.com/B/tu",
        "http://example.com/A/tu",
    ]


def test_alerts_ticks_round_robin_independently_of_trip_updates() -> None:
    fetch = _FetchRecorder()
    scheduler = QuotaGroupScheduler(
        "test-group-c", _members("A", "B"), fetch, _TEST_BUDGET
    )

    for _ in range(ALERTS_TICK_EVERY):
        asyncio.run(scheduler.tick())

    # Ticks 1-4: trip_updates (A, B, A, B). Tick 5: alerts (A).
    assert fetch.calls == [
        "http://example.com/A/tu",
        "http://example.com/B/tu",
        "http://example.com/A/tu",
        "http://example.com/B/tu",
        "http://example.com/A/alerts",
    ]


def test_alerts_tick_skips_members_without_an_alerts_url() -> None:
    fetch = _FetchRecorder()
    members = _members("A")
    members.append(
        {"name": "B", "realtime_url": "http://example.com/B/tu", "alerts_url": None}
    )
    scheduler = QuotaGroupScheduler("test-group-d", members, fetch, _TEST_BUDGET)

    for _ in range(ALERTS_TICK_EVERY):
        asyncio.run(scheduler.tick())

    alerts_calls = [c for c in fetch.calls if "alerts" in c]
    assert alerts_calls == ["http://example.com/A/alerts"]


def test_alerts_tick_is_a_noop_when_no_member_has_an_alerts_url() -> None:
    fetch = _FetchRecorder()
    members = [
        {"name": "A", "realtime_url": "http://example.com/A/tu", "alerts_url": None}
    ]
    scheduler = QuotaGroupScheduler("test-group-e", members, fetch, _TEST_BUDGET)

    for _ in range(ALERTS_TICK_EVERY):
        asyncio.run(scheduler.tick())

    assert all("alerts" not in c for c in fetch.calls)


def test_tick_with_no_members_is_a_noop() -> None:
    fetch = _FetchRecorder()
    scheduler = QuotaGroupScheduler("test-group-f", [], fetch, _TEST_BUDGET)

    for _ in range(ALERTS_TICK_EVERY):
        asyncio.run(scheduler.tick())

    assert fetch.calls == []


def test_trip_updates_tick_advances_next_update_at() -> None:
    fetch = _FetchRecorder()
    scheduler = QuotaGroupScheduler(
        "test-group-g", _members("A", "B"), fetch, _TEST_BUDGET
    )
    initial_a = scheduler.next_update_at["A"]

    asyncio.run(scheduler.tick())  # A's turn

    assert scheduler.next_update_at["A"] > initial_a


def test_run_survives_a_failed_tick_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # asyncio.sleep(0) is specifically special-cased to always yield
    # control back to the event loop once, even with a zero delay -
    # substituting an AsyncMock here wouldn't necessarily do that (an
    # awaited coroutine that never itself suspends can resolve without
    # ever handing control back), so run()'s background task might never
    # actually get scheduled between our own loop's iterations below.
    # Redirecting every real sleep() call (regardless of requested delay)
    # to a real zero-delay one keeps that guarantee while making the test
    # fast, instead of really waiting out the scheduler's true interval.
    real_sleep = asyncio.sleep

    async def instant_sleep(*args: object, **kwargs: object) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    call_count = {"n": 0}

    async def flaky_fetch(url: str) -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("upstream down")
        return "feed"

    scheduler = QuotaGroupScheduler(
        "test-group-h", _members("A"), flaky_fetch, _TEST_BUDGET
    )

    async def run_a_few_ticks() -> None:
        task = asyncio.create_task(scheduler.run())
        for _ in range(10):
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_a_few_ticks())

    assert call_count["n"] >= 2


def test_next_realtime_update_at_finds_the_right_scheduler() -> None:
    scheduler_a = QuotaGroupScheduler(
        "test-group-i", _members("A"), _FetchRecorder(), _TEST_BUDGET
    )
    scheduler_b = QuotaGroupScheduler(
        "test-group-j", _members("B"), _FetchRecorder(), _TEST_BUDGET
    )

    result = next_realtime_update_at([scheduler_a, scheduler_b], "B")

    assert result == int(scheduler_b.next_update_at["B"])


def test_next_realtime_update_at_returns_none_for_an_ungrouped_system() -> None:
    scheduler_a = QuotaGroupScheduler(
        "test-group-k", _members("A"), _FetchRecorder(), _TEST_BUDGET
    )

    assert next_realtime_update_at([scheduler_a], "NotInAnyGroup") is None
