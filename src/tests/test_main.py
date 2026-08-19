import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest
import requests
from fastapi.sse import ServerSentEvent
from google.protobuf.message import DecodeError

from generated import gtfs_realtime_pb2
from src.main import _fetch_feed, transit_feed
from src.services.schedule_cache import ScheduleCache


async def _first_event(transit_system: str) -> ServerSentEvent:
    agen = transit_feed(transit_system)
    try:
        return await agen.__anext__()
    finally:
        await agen.aclose()


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

    event = asyncio.run(_first_event("BART"))

    assert call_count["n"] == 2
    assert event.data.trip_id == "T1"
