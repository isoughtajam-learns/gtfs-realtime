from enum import StrEnum
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import ForeignKey, ForeignKeyConstraint, UniqueConstraint, Sequence
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Status(StrEnum):
    IN_TRANSIT = "In Transit"
    AT_STOP = "Stopped"


# BaseModels for transient, runtime data
class SimplePosition(BaseModel):
    stop_id: str
    previous: int
    next: int
    status: Status
    stop_name: Optional[str] = None


class TripPosition(SimplePosition):
    trip_id: str
    vehicle: str
    trip_headsign: Optional[str] = None
    color: Optional[str] = None
    text_color: Optional[str] = None


class ORMBase(DeclarativeBase):
    pass


class TransitSystem(ORMBase):
    __tablename__ = "transit_system"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(unique=True)
    realtime_url: Mapped[str]
    schedule_url: Mapped[Optional[str]]
    __table_args__ = (
        UniqueConstraint('name', name='uq_name'),
    )


class Route(ORMBase):
    __tablename__ = "route"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    transit_system_id: Mapped[int] = mapped_column(ForeignKey("transit_system.id"))
    route_id: Mapped[str]
    short_name: Mapped[str] = mapped_column(primary_key=True, unique=True, autoincrement=False)
    long_name: Mapped[str]
    url: Mapped[str]
    color: Mapped[str]
    text_color: Mapped[str]

    __table_args__ = (
        UniqueConstraint('id', 'short_name', name='uq_route_short_name'),
        UniqueConstraint('short_name', name='uq_short_name'),
    )


class Trip(ORMBase):
    __tablename__ = "trip"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trip_id: Mapped[str] = mapped_column(primary_key=True, autoincrement=False)
    transit_system_id: Mapped[int] = mapped_column(ForeignKey("transit_system.id"))
    route_id: Mapped[str]
    name: Mapped[Optional[str]]
    direction_id: Mapped[Optional[int]]
    __table_args__ = (
        ForeignKeyConstraint(["id", "trip_id"], ["trip.id", "trip.trip_id"]),
        UniqueConstraint('transit_system_id', 'trip_id', name='uq_trip'),
    )


class Stop(ORMBase):
    __tablename__ = "stop"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    stop_id: Mapped[str] = mapped_column(primary_key=True, autoincrement=False)
    transit_system_id: Mapped[int] = mapped_column(ForeignKey("transit_system.id"))
    trip_id: Mapped[str]
    name: Mapped[str]
    zone_id: Mapped[str]
    stop_headsign: Mapped[Optional[str]]
    __table_args__ = (
        UniqueConstraint('stop_id', 'transit_system_id', name='uq_transit_stop_id'),
    )


# class StaticTransitFeed(ORMBase):
#     """
#     Static fields used by transit feed, allowing a single query to hydrate feed events
#     """
#     __tablename__ = "static_transit_feed"
#     id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
#     transit_system_id: Mapped[int] = mapped_column(ForeignKey("transit_system.id"))
#     trip_id: Mapped[int] = mapped_column(primary_key=True)
#     trip_headsign: Mapped[str]
#     stop_id: Mapped[int]
#     stop_name: Mapped[str]
#     route_id: Mapped[str]
#     route_color: Mapped[str]
#     route_text_color: Mapped[str]
#     __table_args__ = (
#         UniqueConstraint(
#             'transit_system_id', 'trip_id', 'route_id',
#             name='uq_transit_log_natural_key',
#         ),
#     )