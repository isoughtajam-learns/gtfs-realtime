import asyncio
from typing import Any, AsyncGenerator

import requests
from fastapi import FastAPI, HTTPException
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette import status
from fastapi.middleware.cors import CORSMiddleware

from generated import gtfs_realtime_pb2
from src.constants import GTFS_URLS
from src.services.positioning import get_location
from src.models import TripPosition
from src.services.schedule_cache import ScheduleCache
from src.settings import get_settings

app = FastAPI()

# Define the origins that are allowed to make requests to your backend
origins = [
    "http://localhost:3000",  # Default Create React App port
    "http://localhost:5173",  # Default Vite port
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # Allows specific origins
    allow_credentials=True,  # Allows cookies and credentials
    allow_methods=["*"],  # Allows all HTTP methods (GET, POST, etc.)
    allow_headers=["*"],  # Allows all headers
)


async def _fetch_feed(gtfs_url: str) -> gtfs_realtime_pb2.FeedMessage:
    """Fetches and parses one GTFS-Realtime poll. Raises
    requests.exceptions.RequestException on any network/HTTP failure
    (including a non-2xx status, via raise_for_status()) or
    google.protobuf.message.DecodeError on malformed protobuf bytes - both
    are source errors transit_feed() must catch without ending the SSE
    stream for connected clients."""
    response = await asyncio.to_thread(requests.get, gtfs_url, timeout=30)
    response.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    return feed


@app.get("/trip_updates/{transit_system}", response_class=EventSourceResponse)
async def transit_feed(transit_system: str) -> AsyncGenerator[ServerSentEvent, None]:
    gtfs_url = GTFS_URLS.get(transit_system)
    if not gtfs_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    while True:
        (
            trip_headsigns,
            stop_names,
            headsigns_by_route_dir,
            colors_by_trip,
            colors_by_route,
            colors_by_stop,
        ) = await ScheduleCache.get(transit_system)
        try:
            feed = await _fetch_feed(gtfs_url)
        except requests.exceptions.RequestException as ex:
            print(f"Request error fetching feed for {transit_system}: {ex}")
            await asyncio.sleep(30)
            continue
        except Exception as ex:
            print(f"Parse error with Feed Message for {transit_system}: {ex}")
            await asyncio.sleep(30)
            continue

        for entity in feed.entity:
            if entity.HasField("trip_update"):
                trip_descriptor = entity.trip_update.trip
                trip_id = trip_descriptor.trip_id
                route_id = trip_descriptor.route_id
                direction_id = (
                    trip_descriptor.direction_id
                    if trip_descriptor.HasField("direction_id")
                    else None
                )
                vehicle = str(entity.trip_update.vehicle.label)
                stop_time_updates = entity.trip_update.stop_time_update
                position = get_location(stop_time_updates)
                if not position:
                    continue

                destination_stop_id = (
                    stop_time_updates[-1].stop_id
                    if len(stop_time_updates) > 0
                    else None
                )
                destination_headsign = (
                    stop_names.get(destination_stop_id) if destination_stop_id else None
                )
                headsign = (
                    trip_headsigns.get(trip_id)
                    or headsigns_by_route_dir.get((route_id, direction_id))
                    or destination_headsign
                )
                colors = (
                    colors_by_trip.get(trip_id)
                    or colors_by_route.get(route_id)
                    or (
                        colors_by_stop.get(destination_stop_id)
                        if destination_stop_id
                        else None
                    )
                )
                color, text_color = colors if colors else (None, None)
                yield ServerSentEvent(
                    data=TripPosition(
                        trip_id=trip_id,
                        stop_id=position.stop_id,
                        previous=position.previous,
                        next=position.next,
                        vehicle=vehicle,
                        status=position.status,
                        trip_headsign=headsign,
                        stop_name=stop_names.get(position.stop_id),
                        color=color,
                        text_color=text_color,
                    ),
                    event="trip_update",
                    retry=5000,
                )
        await asyncio.sleep(30)


@app.get("/transit_systems")
async def get_transit_systems() -> list[str]:
    return list(GTFS_URLS.keys())


@app.get("/info")
async def info() -> dict[str, Any]:
    settings = get_settings()
    return {
        "app_name": settings.app_name,
        "admin_email": settings.admin_email,
        "env": settings.env,
        "debug_mode": settings.debug,
    }
