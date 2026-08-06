from datetime import datetime

from src.models import SimplePosition, Status


def get_location(stop_times) -> SimplePosition | None:
    prev = None
    for next in stop_times:
        if not prev:
            prev = next
            continue

        should_publish = False
        if prev.departure.time < datetime.now().timestamp() < next.arrival.time:
            before = prev.departure.time
            after = next.arrival.time
            status = Status.IN_TRANSIT
            should_publish = True
            if prev.departure.time > datetime.now().timestamp():
                print("Departure too late")
                import pdb
                pdb.set_trace()

        elif prev.arrival.time < datetime.now().timestamp() < next.departure.time:
            before = prev.arrival.time
            after = next.departure.time
            status = Status.AT_STOP
            should_publish = True


        if should_publish:
            return SimplePosition(
                stop_id=next.stop_id,
                last_arrival=before,
                next_arrival=after,
                status=status,
            )
        prev = next
    return None
