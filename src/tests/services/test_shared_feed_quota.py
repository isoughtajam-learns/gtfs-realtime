import time

import pytest

from src.services import shared_feed_quota as quota_module
from src.services.shared_feed_quota import QUOTA_GROUP_LIMITS, SharedFeedQuota


@pytest.fixture(autouse=True)  # type: ignore[misc]
def _clear_quota_state() -> None:
    # SharedFeedQuota is class-level global state - clear it before every
    # test so one test's consumed budget can't leak into another's.
    SharedFeedQuota._timestamps.clear()


def test_unrecognized_quota_group_always_allowed() -> None:
    # Only a name deliberately configured in QUOTA_GROUP_LIMITS should ever
    # actually constrain anything - a typo or a not-yet-configured group
    # fails open rather than silently blocking every fetch for it.
    for _ in range(1000):
        assert SharedFeedQuota.try_consume("not-a-real-group") is True


def test_allows_up_to_the_configured_limit() -> None:
    limit = QUOTA_GROUP_LIMITS["511.org"]

    results = [SharedFeedQuota.try_consume("511.org") for _ in range(limit)]

    assert all(results)


def test_blocks_once_the_limit_is_reached() -> None:
    limit = QUOTA_GROUP_LIMITS["511.org"]
    for _ in range(limit):
        assert SharedFeedQuota.try_consume("511.org") is True

    assert SharedFeedQuota.try_consume("511.org") is False


def test_different_groups_have_independent_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(quota_module.QUOTA_GROUP_LIMITS, "some-other-group", 2)

    assert SharedFeedQuota.try_consume("some-other-group") is True
    assert SharedFeedQuota.try_consume("some-other-group") is True
    assert SharedFeedQuota.try_consume("some-other-group") is False
    # A separate group's budget is untouched by the above.
    assert SharedFeedQuota.try_consume("511.org") is True


def test_old_timestamps_outside_the_window_are_forgotten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(quota_module.QUOTA_GROUP_LIMITS, "expiring-group", 1)
    now = {"value": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["value"])

    assert SharedFeedQuota.try_consume("expiring-group") is True
    assert SharedFeedQuota.try_consume("expiring-group") is False

    now["value"] += quota_module.WINDOW_SECONDS + 1
    assert SharedFeedQuota.try_consume("expiring-group") is True
