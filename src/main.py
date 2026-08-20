import asyncio
import atexit
from contextlib import asynccontextmanager
import logging
from typing import Any, AsyncGenerator, AsyncIterator

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from posthog import Posthog
from starlette import status

from generated import gtfs_realtime_pb2
from src.constants import GTFS_URLS
from src.models import TripPosition
from src.services.positioning import get_location
from src.services.schedule_cache import ScheduleCache
from src.settings import get_settings
from src.telemetry import configure_posthog_logging

configure_posthog_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize and gracefully stop the shared PostHog client."""
    settings = get_settings()
    posthog_client: Posthog | None = None

    if not settings.posthog_enabled:
        yield
        return

    if not settings.posthog_project_token:
        if not settings.debug:
            raise RuntimeError(
                "POSTHOG_PROJECT_TOKEN variable required by PostHog is missing or "
                "un-configured, this causes events to be silently missed. This error "
                "stops appearing once POSTHOG_PROJECT_TOKEN is configured"
            )
    elif not settings.posthog_host:
        if not settings.debug:
            raise RuntimeError(
                "POSTHOG_HOST variable required by PostHog is missing or un-configured, "
                "this causes events to be silently missed. This error stops appearing "
                "once POSTHOG_HOST is configured"
            )
    else:
        posthog_client = Posthog(
            settings.posthog_project_token,
            host=settings.posthog_host,
            enable_exception_autocapture=True,
        )
        app.state.posthog_client = posthog_client
        atexit.register(posthog_client.shutdown)

    yield

    if posthog_client:
        posthog_client.flush()
        posthog_client.shutdown()
        atexit.unregister(posthog_client.shutdown)


app = FastAPI(lifespan=lifespan)


@app.exception_handler(Exception)
async def capture_unhandled_exception(
    request: Request, exc: Exception
) -> PlainTextResponse:
    """Send unhandled server exceptions to PostHog before returning a 500 response."""
    posthog_client = getattr(request.app.state, "posthog_client", None)
    if posthog_client:
        posthog_client.capture_exception(exc)

    return PlainTextResponse("Internal Server Error", status_code=500)


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
        logger.error(f"GTFS URL not found for {transit_system}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    posthog_client = getattr(app.state, "posthog_client", None)
    if posthog_client:
        posthog_client.capture(
            "trip_updates_stream_opened",
            distinct_id=None,
            properties={"transit_system": transit_system},
        )

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
            logger.error(f"Request error fetching feed for {transit_system}: {ex}")
            await asyncio.sleep(30)
            continue
        except Exception as ex:
            logger.error(f"Parse error with Feed Message for {transit_system}: {ex}")
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
async def get_transit_systems(request: Request) -> list[str]:
    posthog_client = getattr(request.app.state, "posthog_client", None)
    if posthog_client:
        posthog_client.capture(
            "transit_systems_listed",
            distinct_id=None,
        )

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
