from enum import StrEnum

from pydantic import BaseModel


class Status(StrEnum):
    IN_TRANSIT = "in_transit"
    AT_STOP = "at_stop"

class SimplePosition(BaseModel):
    stop_id: str
    last_arrival: int
    next_arrival: int
    status: Status

class TripPosition(SimplePosition):
    trip_id: str
    vehicle: str
