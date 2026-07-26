from datetime import datetime

from src.models import SimplePosition, Status


def get_location(stop_times) -> SimplePosition | None:
    prev = None
    for next in stop_times:
        if not prev:
            prev = next
            continue

        if prev.departure.time < datetime.now().timestamp() < next.arrival.time:
            return SimplePosition(
                stop_id=next.stop_id,
                last_arrival=prev.departure.time,
                next_arrival=next.arrival.time,
                status=Status.IN_TRANSIT
            )

        elif next.arrival.time < datetime.now().timestamp() < next.departure.time:
            return SimplePosition(
                stop_id=next.stop_id,
                last_arrival=next.arrival.time,
                next_arrival=next.departure.time,
                status=Status.AT_STOP,
            )
        prev = next
    return None
