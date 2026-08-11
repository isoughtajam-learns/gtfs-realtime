from datetime import datetime

from generated import gtfs_realtime_pb2
from src.models import Status
from src.positioning import get_location

StopTimeEvent = gtfs_realtime_pb2.TripUpdate.StopTimeEvent
StopTimeUpdate = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate


def _now() -> int:
    return int(datetime.now().timestamp())


def test_unset_arrival_time_does_not_match_at_stop():
    now = _now()
    stop = StopTimeUpdate(
        stop_id="S1",
        departure=StopTimeEvent(time=now + 3600),
    )
    assert get_location([stop]) is None


def test_delay_only_arrival_does_not_match_at_stop():
    now = _now()
    stop = StopTimeUpdate(stop_id="S1")
    stop.arrival.delay = 120
    stop.departure.time = now + 3600
    assert get_location([stop]) is None


def test_both_times_set_at_stop():
    now = _now()
    stop = StopTimeUpdate(
        stop_id="S1",
        arrival=StopTimeEvent(time=now - 30),
        departure=StopTimeEvent(time=now + 30),
    )
    result = get_location([stop])
    assert result is not None
    assert result.status == Status.AT_STOP
    assert result.stop_id == "S1"
    assert result.previous == now - 30
    assert result.next == now + 30


def test_in_transit_between_stops():
    now = _now()
    prev_stop = StopTimeUpdate(
        stop_id="S1",
        departure=StopTimeEvent(time=now - 60),
    )
    next_stop = StopTimeUpdate(
        stop_id="S2",
        arrival=StopTimeEvent(time=now + 60),
    )
    result = get_location([prev_stop, next_stop])
    assert result is not None
    assert result.status == Status.IN_TRANSIT
    assert result.stop_id == "S2"
    assert result.previous == now - 60
    assert result.next == now + 60
