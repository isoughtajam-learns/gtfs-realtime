from datetime import datetime
from typing import Sequence

from generated import gtfs_realtime_pb2
from src.models import SimplePosition, Status

StopTimeEvent = gtfs_realtime_pb2.TripUpdate.StopTimeEvent
StopTimeUpdate = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate


def _event_time(event: StopTimeEvent | None) -> int | None:
    if event is not None and event.HasField("time"):
        return event.time
    return None


def get_location(stop_times: Sequence[StopTimeUpdate]) -> SimplePosition | None:
    now = datetime.now().timestamp()
    prev = None
    for stop in stop_times:
        arrival = _event_time(stop.arrival if stop.HasField("arrival") else None)
        departure = _event_time(stop.departure if stop.HasField("departure") else None)

        if arrival is not None and departure is not None and arrival < now < departure:
            return SimplePosition(
                stop_id=stop.stop_id,
                previous=arrival,
                next=departure,
                status=Status.AT_STOP,
            )
        if prev is not None:
            prev_departure = _event_time(
                prev.departure if prev.HasField("departure") else None
            )
            if (
                prev_departure is not None
                and arrival is not None
                and prev_departure < now < arrival
            ):
                return SimplePosition(
                    stop_id=stop.stop_id,
                    previous=prev_departure,
                    next=arrival,
                    status=Status.IN_TRANSIT,
                )
        prev = stop
    return None
