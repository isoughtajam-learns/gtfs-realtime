import asyncio
from datetime import datetime
from typing import Any, Generator
from unittest.mock import AsyncMock, Mock

import pytest
import requests
from fastapi import HTTPException
from fastapi.sse import ServerSentEvent
from google.protobuf.message import DecodeError

from generated import gtfs_realtime_pb2
from src.main import (
    _entity_trip_id,
    _fetch_feed,
    get_transit_system_detail,
    get_transit_systems,
    service_alerts,
    transit_feed,
    trip_detail,
)
from src.models import (
    AffectedRoute,
    AffectedTrip,
    AlertActivePeriod,
    Status,
    TripPosition,
)
from src.services.recent_events_cache import RecentEventsCache
from src.services.schedule_cache import ScheduleCache


@pytest.fixture(autouse=True)  # type: ignore[misc]
def _clear_recent_events_cache() -> Generator[None, None, None]:
    # RecentEventsCache is class-level global state, same as
    # RealtimeFeedCache/ScheduleCache - unlike those, several tests below
    # reuse the same transit_system name (e.g. "BART"), so without this a
    # later test's initial burst could pick up an earlier test's cached
    # positions instead of exercising a clean stream.
    RecentEventsCache._events.clear()
    yield
    RecentEventsCache._events.clear()


async def _first_event(transit_system: str) -> ServerSentEvent:
    agen = transit_feed(transit_system)
    try:
        return await agen.__anext__()
    finally:
        await agen.aclose()


def _mock_transit_system_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "realtime_url": "http://example.com/feed",
        "schedule_url": "http://example.com/gtfs.zip",
        "timezone": None,
        "default_schedule_url": None,
        "auth_required": False,
        "min_poll_interval_seconds": 0,
        "alerts_url": None,
    }
    config.update(overrides)
    return config


def _active_for(*names: str) -> Any:
    """monkeypatch target for src.main.get_transit_system_config - returns a
    config for any of `names`, None (unknown/inactive system) otherwise."""
    return lambda name: _mock_transit_system_config() if name in names else None


def _mock_response(content: bytes, status_code: int = 200) -> Mock:
    response = Mock()
    response.content = content
    response.status_code = status_code
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"{status_code} error", response=response
        )
    else:
        response.raise_for_status.side_effect = None
    return response


def _valid_feed_bytes() -> bytes:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    entity = feed.entity.add()
    entity.id = "e1"
    entity.trip_update.trip.trip_id = "T1"
    entity.trip_update.trip.route_id = "R1"
    return bytes(feed.SerializeToString())


def test_fetch_feed_raises_request_exception_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A source that's unreachable (DNS failure, connection refused, etc.)
    # must surface as a RequestException - transit_feed() relies on this to
    # distinguish "retry the poll" from "the bytes we got don't parse".
    def boom(*args: object, **kwargs: object) -> None:
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "get", boom)

    with pytest.raises(requests.exceptions.RequestException):
        asyncio.run(_fetch_feed("http://example.com/feed"))


def test_fetch_feed_raises_request_exception_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise requests.exceptions.Timeout("timed out")

    monkeypatch.setattr(requests, "get", boom)

    with pytest.raises(requests.exceptions.RequestException):
        asyncio.run(_fetch_feed("http://example.com/feed"))


def test_fetch_feed_raises_request_exception_on_http_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-2xx status (e.g. the source is down or the URL is wrong) must
    # not be silently handed to ParseFromString as if it were feed bytes -
    # raise_for_status() turns it into a RequestException instead.
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _mock_response(b"<html>error page</html>", status_code=500),
    )

    with pytest.raises(requests.exceptions.RequestException):
        asyncio.run(_fetch_feed("http://example.com/feed"))


def test_fetch_feed_raises_decode_error_on_malformed_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _mock_response(
            b"not a protobuf at all, just plain text garbage"
        ),
    )

    with pytest.raises(DecodeError):
        asyncio.run(_fetch_feed("http://example.com/feed"))


def test_fetch_feed_empty_body_parses_to_zero_entities_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Protobuf treats an empty payload as a valid, all-defaults message
    # rather than a parse error - this is a real GTFS-RT edge case (a source
    # with nothing new to report), not a source error transit_feed() should
    # log or retry over.
    monkeypatch.setattr(requests, "get", lambda *a, **k: _mock_response(b""))

    feed = asyncio.run(_fetch_feed("http://example.com/feed"))

    assert list(feed.entity) == []


def test_fetch_feed_returns_parsed_feed_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _mock_response(_valid_feed_bytes())
    )

    feed = asyncio.run(_fetch_feed("http://example.com/feed"))

    assert len(feed.entity) == 1
    assert feed.entity[0].trip_update.trip.trip_id == "T1"
    assert feed.entity[0].trip_update.trip.route_id == "R1"


def _feed_with_one_active_trip_update() -> bytes:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    entity = feed.entity.add()
    entity.id = "e1"
    entity.trip_update.trip.trip_id = "T1"
    entity.trip_update.trip.route_id = "R1"
    now = int(datetime.now().timestamp())
    stop = entity.trip_update.stop_time_update.add()
    stop.stop_id = "S1"
    stop.arrival.time = now - 30
    stop.departure.time = now + 30
    return bytes(feed.SerializeToString())


def test_transit_feed_survives_a_request_error_and_keeps_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The end-to-end case behind the request: a single bad poll (network
    # blip, source flaking) must not end the SSE stream for connected
    # clients - transit_feed() should log it, wait, and pick back up on the
    # next poll rather than letting the exception propagate out of the
    # generator.
    call_count = {"n": 0}

    def flaky_get(*args: object, **kwargs: object) -> Mock:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise requests.exceptions.ConnectionError("connection refused")
        return _mock_response(_feed_with_one_active_trip_update())

    monkeypatch.setattr(requests, "get", flaky_get)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ScheduleCache, "get", AsyncMock(return_value=({}, {}, {}, {}, {}, {}))
    )
    monkeypatch.setattr("src.main.get_transit_system_config", _active_for("BART"))

    event = asyncio.run(_first_event("BART"))

    assert call_count["n"] == 2
    assert event.data.trip_id == "T1"


def test_transit_feed_yields_cached_recent_events_before_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A newly-connected client shouldn't have to wait out a real poll (let
    # alone this system's rate-limit window) to see anything - the first
    # event(s) come from RecentEventsCache, before requests.get is ever
    # called.
    RecentEventsCache.update(
        "BART",
        [
            TripPosition(
                trip_id="CachedTrip",
                stop_id="S9",
                previous=100,
                next=200,
                status=Status.IN_TRANSIT,
                timestamp=42,
            )
        ],
    )

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("requests.get should not be called for the cached burst")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr("src.main.get_transit_system_config", _active_for("BART"))

    event = asyncio.run(_first_event("BART"))

    assert event.data.trip_id == "CachedTrip"


def test_transit_feed_populates_recent_events_cache_after_a_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RecentEventsCache.update() runs after a poll's whole entity loop - a
    # single grabbed event (_first_event) closes the generator before that
    # point is ever reached, so this needs a second event to force the
    # generator to resume past it (into the update call, the sleep, and the
    # next poll iteration).
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _mock_response(_feed_with_one_active_trip_update()),
    )
    monkeypatch.setattr(
        ScheduleCache, "get", AsyncMock(return_value=({}, {}, {}, {}, {}, {}))
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr("src.main.get_transit_system_config", _active_for("BART"))

    asyncio.run(_first_two_events("BART"))

    cached = RecentEventsCache.get("BART")
    assert [p.trip_id for p in cached] == ["T1"]


def test_entity_trip_id_uses_descriptor_trip_id_when_present() -> None:
    feed = gtfs_realtime_pb2.FeedMessage()
    entity = feed.entity.add()
    entity.id = "e1"
    entity.trip_update.trip.trip_id = "T1"

    assert _entity_trip_id(entity) == "T1"


def test_entity_trip_id_falls_back_to_entity_id_when_trip_id_blank() -> None:
    # Real HSL case: trip_update.trip.trip_id is an empty string on every
    # entity - FeedEntity.id is the only thing that actually distinguishes
    # one vehicle from another.
    feed = gtfs_realtime_pb2.FeedMessage()
    entity = feed.entity.add()
    entity.id = "e1"

    assert _entity_trip_id(entity) == "e1"


def _feed_with_two_blank_trip_id_entities() -> bytes:
    # Mirrors HSL: every entity's trip_id is blank, but each has its own
    # distinct FeedEntity.id and is independently positionable.
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    now = int(datetime.now().timestamp())
    for entity_id, stop_id in (("e1", "S1"), ("e2", "S2")):
        entity = feed.entity.add()
        entity.id = entity_id
        # TripUpdate.trip is a required submessage in the gtfs-realtime
        # proto2 schema - explicitly setting trip_id to "" (not just leaving
        # trip untouched) is what marks it present, mirroring real HSL
        # entities which do include this submessage, just with a blank
        # trip_id inside it.
        entity.trip_update.trip.trip_id = ""
        stop = entity.trip_update.stop_time_update.add()
        stop.stop_id = stop_id
        stop.arrival.time = now - 30
        stop.departure.time = now + 30
    return bytes(feed.SerializeToString())


async def _first_two_events(transit_system: str) -> list[ServerSentEvent]:
    agen = transit_feed(transit_system)
    try:
        return [await agen.__anext__(), await agen.__anext__()]
    finally:
        await agen.aclose()


def test_transit_feed_falls_back_to_entity_id_for_blank_trip_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without the fallback, both entities below would emit trip_id="" and
    # the frontend (which keys live rows by trip_id) would collapse them
    # into a single row - see project memory on the Helsinki live-feed bug.
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _mock_response(_feed_with_two_blank_trip_id_entities()),
    )
    monkeypatch.setattr(
        ScheduleCache, "get", AsyncMock(return_value=({}, {}, {}, {}, {}, {}))
    )
    monkeypatch.setattr(
        "src.main.get_transit_system_config",
        _active_for("Helsinki_Regional_Transport"),
    )

    events = asyncio.run(_first_two_events("Helsinki_Regional_Transport"))

    trip_ids = {event.data.trip_id for event in events}
    assert trip_ids == {"e1", "e2"}


EMPTY_SCHEDULE_CONTEXT: dict[str, Any] = {"trip": None, "route": None, "stops": {}}


def _feed_with_trip_detail_entity() -> bytes:
    """Deliberately mirrors the real BART bug this endpoint was built
    against: the live feed's TripDescriptor.route_id is left unset, even
    though the trip is real and has a route in our own stored Schedule
    data - trip_detail() must fall back to the DB's route_id, not just
    trust (or silently drop) whatever the live feed does or doesn't say."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    entity = feed.entity.add()
    entity.id = "e1"
    entity.trip_update.trip.trip_id = "T1"
    entity.trip_update.trip.direction_id = 1
    entity.trip_update.trip.start_time = "08:00:00"
    entity.trip_update.trip.start_date = "20260901"
    entity.trip_update.vehicle.id = "V1"
    entity.trip_update.vehicle.label = "Vehicle One"
    entity.trip_update.delay = 120
    entity.trip_update.timestamp = 1_700_000_000

    stop1 = entity.trip_update.stop_time_update.add()
    stop1.stop_sequence = 1
    stop1.stop_id = "S1"
    stop1.arrival.time = 1_700_000_100
    stop1.arrival.delay = 60
    stop1.departure.time = 1_700_000_130
    stop1.departure.delay = 60

    stop2 = entity.trip_update.stop_time_update.add()
    stop2.stop_sequence = 2
    stop2.stop_id = "S2"
    stop2.schedule_relationship = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED

    return bytes(feed.SerializeToString())


def _mock_schedule_context() -> dict[str, Any]:
    return {
        "trip": {
            "route_id": "R1",
            "trip_headsign": "Downtown",
            "direction_id": 1,
            "trip_short_name": "509",
            "wheelchair_accessible": 1,
            "bikes_allowed": 2,
        },
        "route": {
            "route_short_name": "Red",
            "route_long_name": "Red Line",
            "route_url": "https://agency.example/red",
            "route_color": "FF0000",
            "route_text_color": "FFFFFF",
            "route_type": 1,
        },
        "stops": {
            "S1": {
                "stop_name": "First St",
                "stop_lat": 37.1,
                "stop_lon": -122.1,
                "platform_code": "1",
                "platform_name": None,
                "wheelchair_boarding": 1,
            },
        },
    }


def test_trip_detail_404s_for_unknown_transit_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.main.get_transit_system_config", _active_for())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(trip_detail("NotASystem", "T1"))
    assert exc_info.value.status_code == 404


def test_trip_detail_502s_on_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr("src.main.get_transit_system_config", _active_for("BART"))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(trip_detail("BART", "T1"))
    assert exc_info.value.status_code == 502


def test_trip_detail_502s_on_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _mock_response(b"not a protobuf, just garbage"),
    )
    monkeypatch.setattr("src.main.get_transit_system_config", _active_for("BART"))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(trip_detail("BART", "T1"))
    assert exc_info.value.status_code == 502


def test_trip_detail_404s_when_trip_not_in_current_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _mock_response(_valid_feed_bytes())
    )
    monkeypatch.setattr("src.main.get_transit_system_config", _active_for("BART"))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(trip_detail("BART", "some-other-trip-id"))
    assert exc_info.value.status_code == 404


def test_trip_detail_matches_entity_by_fallback_id_when_trip_id_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real HSL case: trip_update.trip.trip_id is blank, so /trip_detail has
    # to be looked up by the same entity-id fallback transit_feed() emits -
    # matching on the raw (blank) descriptor trip_id would just grab
    # whichever blank-trip_id entity happens to come first in the feed.
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _mock_response(_feed_with_two_blank_trip_id_entities()),
    )
    monkeypatch.setattr(
        "src.main.get_trip_schedule_context", lambda *a, **k: EMPTY_SCHEDULE_CONTEXT
    )
    monkeypatch.setattr("src.main.get_scheduled_tail_stops", lambda *a, **k: [])
    monkeypatch.setattr(
        "src.main.get_transit_system_config",
        _active_for("Helsinki_Regional_Transport"),
    )

    detail = asyncio.run(trip_detail("Helsinki_Regional_Transport", "e2"))

    assert detail.trip_id == "e2"
    assert detail.stops[0].stop_id == "S2"


def test_trip_detail_merges_live_feed_with_schedule_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _mock_response(_feed_with_trip_detail_entity()),
    )
    monkeypatch.setattr(
        "src.main.get_trip_schedule_context",
        lambda *a, **k: _mock_schedule_context(),
    )
    # This test isn't about the tail-stop backfill - stub it out so it can't
    # accidentally pass or fail based on whatever real stop_times.txt happens
    # to be sitting in src/metadata/BART/ on whichever machine runs it.
    monkeypatch.setattr("src.main.get_scheduled_tail_stops", lambda *a, **k: [])
    monkeypatch.setattr("src.main.get_transit_system_config", _active_for("BART"))

    detail = asyncio.run(trip_detail("BART", "T1"))

    assert detail.trip_id == "T1"
    # TripDescriptor.route_id was never set on the live entity - this is
    # only non-None because it fell back to the DB's stored route_id.
    assert detail.route_id == "R1"
    assert detail.direction_id == 1
    assert detail.trip_headsign == "Downtown"
    assert detail.trip_short_name == "509"
    assert detail.wheelchair_accessible == 1
    assert detail.bikes_allowed == 2
    assert detail.start_time == "08:00:00"
    assert detail.start_date == "20260901"
    assert detail.schedule_relationship == "SCHEDULED"
    assert detail.delay == 120
    assert detail.timestamp == 1_700_000_000
    assert detail.vehicle_id == "V1"
    assert detail.vehicle_label == "Vehicle One"
    assert detail.route_short_name == "Red"
    assert detail.route_long_name == "Red Line"
    assert detail.route_color == "FF0000"
    assert detail.route_type == 1

    assert len(detail.stops) == 2
    first, second = detail.stops

    assert first.stop_id == "S1"
    assert first.stop_sequence == 1
    assert first.stop_name == "First St"
    assert first.stop_lat == 37.1
    assert first.stop_lon == -122.1
    assert first.platform_code == "1"
    assert first.wheelchair_boarding == 1
    assert first.arrival_time == 1_700_000_100
    assert first.arrival_delay == 60
    assert first.departure_time == 1_700_000_130
    assert first.departure_delay == 60
    assert first.schedule_relationship == "SCHEDULED"

    # S2 has no Schedule-side match and no arrival/departure - every
    # Schedule-derived field should degrade to None rather than error,
    # and the live SKIPPED status must come through correctly.
    assert second.stop_id == "S2"
    assert second.stop_name is None
    assert second.arrival_time is None
    assert second.departure_time is None
    assert second.schedule_relationship == "SKIPPED"


def test_trip_detail_missing_schedule_context_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trip that's live right now but wasn't in our last Schedule fetch
    (e.g. newly added) should still return a usable response - just
    without the Schedule-derived fields, not a crash."""
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _mock_response(_feed_with_trip_detail_entity()),
    )
    monkeypatch.setattr(
        "src.main.get_trip_schedule_context",
        lambda *a, **k: EMPTY_SCHEDULE_CONTEXT,
    )
    monkeypatch.setattr("src.main.get_scheduled_tail_stops", lambda *a, **k: [])
    monkeypatch.setattr("src.main.get_transit_system_config", _active_for("BART"))

    detail = asyncio.run(trip_detail("BART", "T1"))

    assert detail.trip_id == "T1"
    assert detail.route_id is None
    assert detail.trip_headsign is None
    assert detail.route_short_name is None
    assert detail.stops[0].stop_name is None


def test_trip_detail_backfills_tail_stops_missing_from_live_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the live feed stops short of the end of the line (confirmed
    live: BART), the response should still cover the rest of the route
    from our stored Schedule data - each backfilled stop marked NO_DATA
    with no arrival/departure info, since none was ever published."""
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _mock_response(_feed_with_trip_detail_entity()),
    )
    monkeypatch.setattr(
        "src.main.get_scheduled_tail_stops",
        lambda transit_system, trip_id, live_stop_ids: (
            [(3, "S3"), (4, "S4")] if live_stop_ids == ["S1", "S2"] else []
        ),
    )

    def _schedule_context(
        transit_system: str, trip_id: str, stop_ids: list[str]
    ) -> dict[str, Any]:
        context = _mock_schedule_context()
        # The endpoint must ask for tail stops too, not just the live ones.
        assert set(stop_ids) == {"S1", "S2", "S3", "S4"}
        context["stops"]["S3"] = {
            "stop_name": "Third St",
            "stop_lat": 37.3,
            "stop_lon": -122.3,
            "platform_code": "3",
            "platform_name": None,
            "wheelchair_boarding": 1,
        }
        return context

    monkeypatch.setattr("src.main.get_trip_schedule_context", _schedule_context)
    monkeypatch.setattr("src.main.get_transit_system_config", _active_for("BART"))

    detail = asyncio.run(trip_detail("BART", "T1"))

    assert len(detail.stops) == 4
    third, fourth = detail.stops[2], detail.stops[3]

    assert third.stop_id == "S3"
    assert third.stop_sequence == 3
    assert third.stop_name == "Third St"
    assert third.stop_lat == 37.3
    assert third.arrival_time is None
    assert third.arrival_delay is None
    assert third.departure_time is None
    assert third.departure_delay is None
    assert third.schedule_relationship == "NO_DATA"

    # S4 has no Schedule-side match either - should degrade to None, not error.
    assert fourth.stop_id == "S4"
    assert fourth.stop_sequence == 4
    assert fourth.stop_name is None
    assert fourth.schedule_relationship == "NO_DATA"


def test_transit_system_detail_404s_for_unknown_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.main.get_transit_system_config", _active_for())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_transit_system_detail("NotASystem"))
    assert exc_info.value.status_code == 404


def test_transit_system_detail_returns_full_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_transit_system_config",
        lambda name: (
            _mock_transit_system_config(
                timezone="America/Los_Angeles",
                default_schedule_url="https://www.bart.gov/schedules",
                auth_required=True,
            )
            if name == "BART"
            else None
        ),
    )

    detail = asyncio.run(get_transit_system_detail("BART"))

    assert detail.name == "BART"
    assert detail.timezone == "America/Los_Angeles"
    assert detail.default_schedule_url == "https://www.bart.gov/schedules"
    assert detail.auth_required is True


def test_transit_system_detail_degrades_gracefully_when_fields_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A system that's configured but hasn't had a successful Schedule fetch
    # yet (or whose agency.txt didn't parse) should degrade to None/False,
    # not error - matches how every other Schedule-derived field behaves.
    monkeypatch.setattr("src.main.get_transit_system_config", _active_for("BART"))

    detail = asyncio.run(get_transit_system_detail("BART"))

    assert detail.name == "BART"
    assert detail.timezone is None
    assert detail.default_schedule_url is None
    assert detail.auth_required is False


def test_get_transit_systems_returns_active_system_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.main.get_active_transit_systems",
        lambda: [
            {
                "name": "BART",
                "realtime_url": "http://example.com/bart",
                "schedule_url": "http://example.com/bart.zip",
                "auth_required": False,
            },
            {
                "name": "MBTA",
                "realtime_url": "http://example.com/mbta",
                "schedule_url": "http://example.com/mbta.zip",
                "auth_required": False,
            },
        ],
    )

    request = Mock()
    request.app.state = Mock(spec=[])
    systems = asyncio.run(get_transit_systems(request))

    assert systems == ["BART", "MBTA"]


def _active_with_alerts(*names: str) -> Any:
    """monkeypatch target for src.main.get_transit_system_config - like
    _active_for, but with alerts_url populated, since service_alerts() 404s
    on a system with no alerts feed configured even if it's otherwise
    active."""
    return lambda name: (
        _mock_transit_system_config(alerts_url="http://example.com/alerts")
        if name in names
        else None
    )


def _valid_alerts_feed_bytes() -> bytes:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    entity = feed.entity.add()
    entity.id = "A1"
    alert = entity.alert
    alert.cause = gtfs_realtime_pb2.Alert.MAINTENANCE
    alert.effect = gtfs_realtime_pb2.Alert.DETOUR
    alert.severity_level = gtfs_realtime_pb2.Alert.WARNING
    alert.header_text.translation.add(text="Weekend maintenance", language="en")
    alert.description_text.translation.add(
        text="Trains replaced by shuttle buses", language="en"
    )
    alert.url.translation.add(text="http://example.com/alert-details", language="en")
    period = alert.active_period.add()
    period.start = 1_700_000_000
    period.end = 1_700_100_000
    informed = alert.informed_entity.add()
    informed.trip.trip_id = "T1"
    return feed.SerializeToString()


def test_service_alerts_404s_for_unknown_transit_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.main.get_transit_system_config", _active_with_alerts())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(service_alerts("NotASystem"))
    assert exc_info.value.status_code == 404


def test_service_alerts_404s_when_no_alerts_url_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # BART is active and has a realtime_url, but _active_for's default
    # config leaves alerts_url unset - same 404 as an unknown system, since
    # there's nothing for this endpoint to fetch either way.
    monkeypatch.setattr("src.main.get_transit_system_config", _active_for("BART"))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(service_alerts("BART"))
    assert exc_info.value.status_code == 404


def test_service_alerts_502s_on_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(
        "src.main.get_transit_system_config", _active_with_alerts("BART")
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(service_alerts("BART"))
    assert exc_info.value.status_code == 502


def test_service_alerts_502s_on_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _mock_response(b"not a protobuf, just garbage"),
    )
    monkeypatch.setattr(
        "src.main.get_transit_system_config", _active_with_alerts("BART")
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(service_alerts("BART"))
    assert exc_info.value.status_code == 502


def test_service_alerts_returns_hydrated_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _mock_response(_valid_alerts_feed_bytes())
    )
    monkeypatch.setattr(
        "src.main.get_transit_system_config", _active_with_alerts("BART")
    )
    monkeypatch.setattr(
        "src.main.get_alert_hydration_data",
        lambda transit_system, trip_ids, route_ids: (
            {"T1": {"trip_headsign": "Downtown", "route_id": "R1"}},
            {"R1": {"route_short_name": "Red", "route_long_name": "Red Line"}},
        ),
    )

    alerts = asyncio.run(service_alerts("BART"))

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.alert_id == "A1"
    assert alert.cause == "MAINTENANCE"
    assert alert.effect == "DETOUR"
    assert alert.severity_level == "WARNING"
    assert alert.header_text == "Weekend maintenance"
    assert alert.description_text == "Trains replaced by shuttle buses"
    assert alert.url == "http://example.com/alert-details"
    assert alert.active_period == [
        AlertActivePeriod(start=1_700_000_000, end=1_700_100_000)
    ]
    assert alert.affected_trips == [
        AffectedTrip(trip_id="T1", trip_headsign="Downtown", route_id="R1")
    ]
    # R1 was only referenced through T1's own route_id, not directly in
    # any informed_entity - still expected to show up, hydrated.
    assert alert.affected_routes == [
        AffectedRoute(route_id="R1", route_short_name="Red", route_long_name="Red Line")
    ]


def test_service_alerts_degrades_gracefully_without_hydration_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trip_id/route_id the live alert names but we have no Schedule data
    for should still show up, just without the hydrated fields - not get
    dropped or error."""
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _mock_response(_valid_alerts_feed_bytes())
    )
    monkeypatch.setattr(
        "src.main.get_transit_system_config", _active_with_alerts("BART")
    )
    monkeypatch.setattr("src.main.get_alert_hydration_data", lambda *a, **k: ({}, {}))

    alerts = asyncio.run(service_alerts("BART"))

    assert len(alerts) == 1
    assert alerts[0].affected_trips == [
        AffectedTrip(trip_id="T1", trip_headsign=None, route_id=None)
    ]
    assert alerts[0].affected_routes == []
