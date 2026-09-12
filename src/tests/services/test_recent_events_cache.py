from src.models import Status, TripPosition
from src.services.recent_events_cache import MAX_EVENTS, RecentEventsCache


def _position(trip_id: str, timestamp: int | None) -> TripPosition:
    return TripPosition(
        trip_id=trip_id,
        stop_id="S1",
        previous=100,
        next=200,
        status=Status.IN_TRANSIT,
        timestamp=timestamp,
    )


def test_get_returns_empty_list_for_unknown_system() -> None:
    assert RecentEventsCache.get("no-such-system") == []


def test_update_then_get_round_trips() -> None:
    RecentEventsCache.update("system-a", [_position("T1", 100), _position("T2", 200)])

    result = RecentEventsCache.get("system-a")

    assert [p.trip_id for p in result] == ["T1", "T2"]


def test_get_sorts_oldest_first() -> None:
    RecentEventsCache.update(
        "system-b", [_position("T1", 300), _position("T2", 100), _position("T3", 200)]
    )

    result = RecentEventsCache.get("system-b")

    assert [p.trip_id for p in result] == ["T2", "T3", "T1"]


def test_updating_same_trip_id_replaces_rather_than_duplicates() -> None:
    RecentEventsCache.update("system-c", [_position("T1", 100)])
    RecentEventsCache.update("system-c", [_position("T1", 200)])

    result = RecentEventsCache.get("system-c")

    assert len(result) == 1
    assert result[0].timestamp == 200


def test_different_transit_systems_cached_independently() -> None:
    RecentEventsCache.update("system-d", [_position("T1", 100)])
    RecentEventsCache.update("system-e", [_position("T2", 100)])

    assert [p.trip_id for p in RecentEventsCache.get("system-d")] == ["T1"]
    assert [p.trip_id for p in RecentEventsCache.get("system-e")] == ["T2"]


def test_none_timestamp_sorts_as_oldest_without_erroring() -> None:
    RecentEventsCache.update("system-f", [_position("T1", None), _position("T2", 50)])

    result = RecentEventsCache.get("system-f")

    assert [p.trip_id for p in result] == ["T1", "T2"]


def test_caps_at_max_events_keeping_the_newest() -> None:
    # One trip per timestamp 0..MAX_EVENTS (MAX_EVENTS + 1 distinct trips
    # total) - only the newest MAX_EVENTS should survive.
    positions = [_position(f"T{i}", i) for i in range(MAX_EVENTS + 1)]

    RecentEventsCache.update("system-g", positions)

    result = RecentEventsCache.get("system-g")

    assert len(result) == MAX_EVENTS
    # The oldest one (timestamp 0, trip "T0") got evicted.
    assert "T0" not in {p.trip_id for p in result}
    assert [p.trip_id for p in result][-1] == f"T{MAX_EVENTS}"


def test_cap_is_enforced_across_multiple_update_calls() -> None:
    # Same as above, but arriving as MAX_EVENTS + 1 separate poll cycles
    # rather than one big batch - the real usage pattern (update() called
    # once per 30s poll).
    for i in range(MAX_EVENTS + 1):
        RecentEventsCache.update("system-h", [_position(f"T{i}", i)])

    result = RecentEventsCache.get("system-h")

    assert len(result) == MAX_EVENTS
    assert "T0" not in {p.trip_id for p in result}
