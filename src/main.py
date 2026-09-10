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
from src.models import (
    ServiceAlert,
    TransitSystemDetail,
    TripDetail,
    TripPosition,
    TripStopDetail,
)
from src.services.positioning import get_location
from src.services.realtime_feed_cache import RealtimeFeedCache
from src.services.schedule_cache import ScheduleCache
from src.services.service_alerts import (
    build_service_alerts,
    extract_referenced_ids,
    get_alert_hydration_data,
)
from src.services.transit_system_detail import (
    get_active_transit_systems,
    get_transit_system_config,
)
from src.services.trip_detail import get_scheduled_tail_stops, get_trip_schedule_context
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


def _entity_trip_id(entity: gtfs_realtime_pb2.FeedEntity) -> str:
    """The identifier this codebase treats as a trip's identity for both
    the SSE stream and /trip_detail lookups: the realtime feed's own
    TripDescriptor.trip_id when it's set, falling back to the feed
    entity's own top-level `id` when it isn't.

    Confirmed live: Helsinki_Regional_Transport never sets trip_id at all
    - it's blank on every entity (654/654 polled) - which previously made
    every HSL vehicle indistinguishable from every other one downstream
    (the frontend's live feed collapsed to a single row, since it keys
    trips by trip_id). FeedEntity.id is unique per trip and stable across
    polls for HSL (confirmed live, 934/934 unchanged across a 15s gap),
    so it's a safe fallback specifically for the "what row is this"
    identity - NOT used for ScheduleCache/DB trip_id lookups, which stay
    keyed on the raw (possibly blank) descriptor value since a feed
    entity id was never in Schedule data to begin with."""
    return entity.trip_update.trip.trip_id or entity.id


@app.get("/trip_updates/{transit_system}", response_class=EventSourceResponse)
async def transit_feed(transit_system: str) -> AsyncGenerator[ServerSentEvent, None]:
    config = await asyncio.to_thread(get_transit_system_config, transit_system)
    if not config:
        logger.error(f"GTFS URL not found for {transit_system}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    gtfs_url = config["realtime_url"]
    min_poll_interval_seconds = config["min_poll_interval_seconds"]

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
            # Every connected client runs this same 30s loop independently -
            # without RealtimeFeedCache, N clients would mean N times the
            # outbound request rate to this system's own API. The cache
            # shares one real fetch across all of them, respecting
            # min_poll_interval_seconds (0 - the default - means never
            # cache, so this is a no-op for every system that doesn't need
            # rate limiting).
            feed = await RealtimeFeedCache.get(
                transit_system,
                lambda: _fetch_feed(gtfs_url),
                min_poll_interval_seconds,
            )
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
                # trip_id used below for the emitted row's identity (falls
                # back to the entity id so HSL's blank trip_id doesn't
                # collapse every vehicle into one row client-side); Schedule
                # cache lookups a few lines down deliberately use
                # trip_descriptor.trip_id directly instead - a feed entity id
                # was never in Schedule data, so falling back there would
                # just look up the wrong key.
                trip_id = _entity_trip_id(entity)
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
                    trip_headsigns.get(trip_descriptor.trip_id)
                    or headsigns_by_route_dir.get((route_id, direction_id))
                    or destination_headsign
                )
                colors = (
                    colors_by_trip.get(trip_descriptor.trip_id)
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


@app.get("/trip_detail/{transit_system}/{trip_id}")
async def trip_detail(transit_system: str, trip_id: str) -> TripDetail:
    """Everything the realtime feed says about one trip right now, plus
    whatever GTFS Schedule data we have on its route/trip/stops - the full
    stop_time_update list (not just the current position, unlike
    transit_feed()'s SSE event), per-stop delay/schedule_relationship,
    vehicle info where the source publishes it, and Schedule fields
    (coordinates, route_type, platform, accessibility) that never made it
    into the streamed event at all.

    `trip_id` is whatever transit_feed() emitted as this trip's identity
    (see _entity_trip_id) - the real TripDescriptor.trip_id for most
    sources, or a feed entity id for a source like HSL that never sets
    one. Schedule-derived fields (route/trip/stop detail, tail-stop
    backfill) still key off the raw descriptor value internally and
    degrade to None/empty for a source where that never matches - see
    get_trip_schedule_context and get_scheduled_tail_stops."""
    config = await asyncio.to_thread(get_transit_system_config, transit_system)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown transit system"
        )
    gtfs_url = config["realtime_url"]

    try:
        # Shares the same cache as transit_feed()'s SSE loop - see
        # RealtimeFeedCache. A busy detail view no longer means an extra
        # uncoordinated poll of the upstream source on top of the SSE
        # stream's own; min_poll_interval_seconds=0 (the default) still
        # fetches fresh on every request, exactly like before.
        feed = await RealtimeFeedCache.get(
            transit_system,
            lambda: _fetch_feed(gtfs_url),
            config["min_poll_interval_seconds"],
        )
    except requests.exceptions.RequestException as ex:
        logger.error(f"Request error fetching feed for {transit_system}: {ex}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the realtime source",
        )
    except Exception as ex:
        logger.error(f"Parse error with Feed Message for {transit_system}: {ex}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Malformed realtime feed"
        )

    entity = next(
        (
            e
            for e in feed.entity
            if e.HasField("trip_update") and _entity_trip_id(e) == trip_id
        ),
        None,
    )
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trip not found in the current realtime feed - it may not be running right now",
        )

    trip_update = entity.trip_update
    trip_descriptor = trip_update.trip
    live_stop_ids = [stu.stop_id for stu in trip_update.stop_time_update if stu.stop_id]

    # Some sources (confirmed live: BART) don't publish a stop_time_update
    # for every remaining stop on a trip - the live feed can legitimately
    # fall short of where the route's long_name says the line actually
    # ends. Backfill the rest from our own stored Schedule data so the
    # response still shows the full route, marked as having no live
    # prediction rather than silently cutting off early.
    tail_stops = (
        await asyncio.to_thread(
            get_scheduled_tail_stops, transit_system, trip_id, live_stop_ids
        )
        if live_stop_ids
        else []
    )
    tail_stop_ids = [stop_id for _, stop_id in tail_stops]

    context = await asyncio.to_thread(
        get_trip_schedule_context,
        transit_system,
        trip_id,
        live_stop_ids + tail_stop_ids,
    )
    trip_schedule = context["trip"] or {}
    route_schedule = context["route"] or {}
    stops_schedule = context["stops"]

    stops = [
        TripStopDetail(
            stop_sequence=(
                stu.stop_sequence if stu.HasField("stop_sequence") else None
            ),
            stop_id=stu.stop_id,
            stop_name=stops_schedule.get(stu.stop_id, {}).get("stop_name"),
            stop_lat=stops_schedule.get(stu.stop_id, {}).get("stop_lat"),
            stop_lon=stops_schedule.get(stu.stop_id, {}).get("stop_lon"),
            platform_code=stops_schedule.get(stu.stop_id, {}).get("platform_code"),
            platform_name=stops_schedule.get(stu.stop_id, {}).get("platform_name"),
            wheelchair_boarding=stops_schedule.get(stu.stop_id, {}).get(
                "wheelchair_boarding"
            ),
            arrival_time=(
                stu.arrival.time
                if stu.HasField("arrival") and stu.arrival.HasField("time")
                else None
            ),
            arrival_delay=(
                stu.arrival.delay
                if stu.HasField("arrival") and stu.arrival.HasField("delay")
                else None
            ),
            departure_time=(
                stu.departure.time
                if stu.HasField("departure") and stu.departure.HasField("time")
                else None
            ),
            departure_delay=(
                stu.departure.delay
                if stu.HasField("departure") and stu.departure.HasField("delay")
                else None
            ),
            schedule_relationship=gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.ScheduleRelationship.Name(
                stu.schedule_relationship
            ),
        )
        for stu in trip_update.stop_time_update
    ]
    stops.extend(
        TripStopDetail(
            stop_sequence=sequence,
            stop_id=stop_id,
            stop_name=stops_schedule.get(stop_id, {}).get("stop_name"),
            stop_lat=stops_schedule.get(stop_id, {}).get("stop_lat"),
            stop_lon=stops_schedule.get(stop_id, {}).get("stop_lon"),
            platform_code=stops_schedule.get(stop_id, {}).get("platform_code"),
            platform_name=stops_schedule.get(stop_id, {}).get("platform_name"),
            wheelchair_boarding=stops_schedule.get(stop_id, {}).get(
                "wheelchair_boarding"
            ),
            arrival_time=None,
            arrival_delay=None,
            departure_time=None,
            departure_delay=None,
            schedule_relationship="NO_DATA",
        )
        for sequence, stop_id in tail_stops
    )

    return TripDetail(
        trip_id=trip_id,
        route_id=trip_descriptor.route_id or trip_schedule.get("route_id"),
        direction_id=(
            trip_descriptor.direction_id
            if trip_descriptor.HasField("direction_id")
            else trip_schedule.get("direction_id")
        ),
        trip_headsign=trip_schedule.get("trip_headsign"),
        trip_short_name=trip_schedule.get("trip_short_name"),
        wheelchair_accessible=trip_schedule.get("wheelchair_accessible"),
        bikes_allowed=trip_schedule.get("bikes_allowed"),
        start_time=trip_descriptor.start_time or None,
        start_date=trip_descriptor.start_date or None,
        schedule_relationship=gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship.Name(
            trip_descriptor.schedule_relationship
        ),
        delay=trip_update.delay if trip_update.HasField("delay") else None,
        timestamp=trip_update.timestamp if trip_update.HasField("timestamp") else None,
        vehicle_id=trip_update.vehicle.id or None,
        vehicle_label=trip_update.vehicle.label or None,
        route_short_name=route_schedule.get("route_short_name"),
        route_long_name=route_schedule.get("route_long_name"),
        route_url=route_schedule.get("route_url"),
        route_color=route_schedule.get("route_color"),
        route_text_color=route_schedule.get("route_text_color"),
        route_type=route_schedule.get("route_type"),
        stops=stops,
    )


@app.get("/service_alerts/{transit_system}")
async def service_alerts(transit_system: str) -> list[ServiceAlert]:
    """Every currently-published GTFS-RT ServiceAlert for this system,
    hydrated against our stored Schedule data - see
    models.TransitSystem.alerts_url and src/services/service_alerts.py.
    404s for a system with no alerts_url configured, same as an unknown
    system - both mean "nothing to fetch here"."""
    config = await asyncio.to_thread(get_transit_system_config, transit_system)
    if not config or not config["alerts_url"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown transit system or no service alerts feed configured",
        )
    alerts_url = config["alerts_url"]

    try:
        # Shares RealtimeFeedCache with transit_feed()/trip_detail() under a
        # distinct key - see RealtimeFeedCache.get's key param - so a
        # system's alerts_url and realtime_url are rate-limited together
        # against min_poll_interval_seconds (one shared quota, e.g. 511.org)
        # without one feed's cache entry clobbering the other's.
        feed = await RealtimeFeedCache.get(
            f"{transit_system}:alerts",
            lambda: _fetch_feed(alerts_url),
            config["min_poll_interval_seconds"],
        )
    except requests.exceptions.RequestException as ex:
        logger.error(f"Request error fetching alerts for {transit_system}: {ex}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the alerts source",
        )
    except Exception as ex:
        logger.error(f"Parse error with alerts Feed Message for {transit_system}: {ex}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Malformed alerts feed"
        )

    trip_ids, route_ids = extract_referenced_ids(feed)
    trips_by_id, routes_by_id = await asyncio.to_thread(
        get_alert_hydration_data, transit_system, list(trip_ids), list(route_ids)
    )
    return [
        ServiceAlert(**alert)
        for alert in build_service_alerts(feed, trips_by_id, routes_by_id)
    ]


@app.get("/transit_systems")
async def get_transit_systems(request: Request) -> list[str]:
    posthog_client = getattr(request.app.state, "posthog_client", None)
    if posthog_client:
        posthog_client.capture(
            "transit_systems_listed",
            distinct_id=None,
        )

    systems = await asyncio.to_thread(get_active_transit_systems)
    return [system["name"] for system in systems]


@app.get("/transit_systems/{transit_system}")
async def get_transit_system_detail(transit_system: str) -> TransitSystemDetail:
    """System-level metadata that changes rarely (timezone, the route_url
    fallback, whether fetching needs an API secret) - its own endpoint
    rather than fields on every SSE event, since the frontend can fetch and
    cache this once instead of receiving the same static values on every
    streamed trip_update."""
    config = await asyncio.to_thread(get_transit_system_config, transit_system)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown transit system"
        )

    return TransitSystemDetail(
        name=transit_system,
        timezone=config["timezone"],
        default_schedule_url=config["default_schedule_url"],
        auth_required=config["auth_required"],
    )


@app.get("/info")
async def info() -> dict[str, Any]:
    settings = get_settings()
    return {
        "app_name": settings.app_name,
        "admin_email": settings.admin_email,
        "env": settings.env,
        "debug_mode": settings.debug,
    }
