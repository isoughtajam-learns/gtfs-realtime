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
    IntraSystemTransfer,
    LineSummary,
    ServiceAlert,
    TransitSystemDetail,
    TripDetail,
    TripPosition,
    TripStopDetail,
)
from src.services.positioning import get_location
from src.services.quota_group_scheduler import (
    QuotaGroupScheduler,
    RegionalFeedScheduler,
    next_realtime_update_at,
)
from src.services.realtime_feed_cache import RealtimeFeedCache
from src.services.recent_events_cache import RecentEventsCache
from src.services.regional_feed import (
    REGIONAL_SERVICE_ALERTS_URL,
    REGIONAL_TRIP_UPDATES_URL,
)
from src.services.schedule_cache import ScheduleCache
from src.services.service_alerts import (
    build_service_alerts,
    extract_referenced_ids,
    get_alert_hydration_data,
)
from src.services.shared_feed_quota import QUOTA_GROUP_LIMITS
from src.services.transfer_cache import TransferCache, resolve_inter_system_transfers
from src.services.transit_system_detail import (
    get_active_quota_groups,
    get_active_transit_systems,
    get_quota_group_members,
    get_regional_feed_members,
    get_transit_system_config,
)
from src.services.trip_detail import (
    get_scheduled_tail_stops,
    get_transfers_for_stops,
    get_trip_schedule_context,
)
from src.settings import get_settings
from src.telemetry import configure_posthog_logging

configure_posthog_logging()
logger = logging.getLogger(__name__)


async def _start_quota_group_schedulers(app: FastAPI) -> list[asyncio.Task[None]]:
    """One background task per active quota_group (currently just
    "511.org") for each of its two possible member types: a
    QuotaGroupScheduler for any member still on its own direct
    realtime_url/alerts_url fetch, and/or a RegionalFeedScheduler for any
    member cut over to the combined agency=RG feed (see
    TransitSystem.realtime_feed_agency_code) - both draw from the same
    SharedFeedQuota budget, so running both at once for a quota_group
    that's mid-migration is safe, just not the steady state. Scheduler
    instances are kept on app.state so get_transit_system_detail() can
    read next_realtime_update_at off them; task handles are returned so
    lifespan() can cancel them cleanly on shutdown."""
    app.state.quota_group_schedulers = []
    tasks: list[asyncio.Task[None]] = []
    groups = await asyncio.to_thread(get_active_quota_groups)
    for group in groups:
        # Unconfigured groups (not in QUOTA_GROUP_LIMITS) have no real
        # budget to schedule against - SharedFeedQuota.try_consume already
        # treats them as unbounded, so there's nothing for a scheduler to
        # usefully pace here.
        budget = QUOTA_GROUP_LIMITS.get(group)
        if budget is None:
            logger.warning(
                f"quota_group {group!r} has no configured budget in "
                "QUOTA_GROUP_LIMITS - not starting a scheduler for it."
            )
            continue

        members = await asyncio.to_thread(get_quota_group_members, group)
        if members:
            scheduler = QuotaGroupScheduler(group, members, _fetch_feed, budget)
            app.state.quota_group_schedulers.append(scheduler)
            tasks.append(asyncio.create_task(scheduler.run()))

        # The regional feed is currently 511.org-specific - the only
        # source this app has a region-wide combined-fetch for (see
        # src/services/regional_feed.py).
        if group == "511.org":
            regional_members = await asyncio.to_thread(get_regional_feed_members, group)
            if regional_members:
                api_key = get_settings().api_key_511_org
                if not api_key:
                    logger.warning(
                        "API_KEY_511_ORG is not set - not starting the regional "
                        f"feed scheduler for {len(regional_members)} system(s) "
                        "cut over to it."
                    )
                else:
                    regional_scheduler = RegionalFeedScheduler(
                        group,
                        regional_members,
                        f"{REGIONAL_TRIP_UPDATES_URL}?api_key={api_key}&agency=RG",
                        f"{REGIONAL_SERVICE_ALERTS_URL}?api_key={api_key}&agency=RG",
                        _fetch_feed,
                        budget,
                    )
                    app.state.quota_group_schedulers.append(regional_scheduler)
                    tasks.append(asyncio.create_task(regional_scheduler.run()))
    return tasks


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize and gracefully stop the shared PostHog client, and start
    (then cancel, on shutdown) one QuotaGroupScheduler task per active
    quota_group - see _start_quota_group_schedulers above. The latter runs
    regardless of PostHog settings, unlike the early-return path below for
    a disabled PostHog client."""
    settings = get_settings()
    posthog_client: Posthog | None = None

    if not settings.posthog_enabled:
        scheduler_tasks = await _start_quota_group_schedulers(app)
        yield
        for task in scheduler_tasks:
            task.cancel()
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

    scheduler_tasks = await _start_quota_group_schedulers(app)

    yield

    for task in scheduler_tasks:
        task.cancel()

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
    # Known benign log noise, confirmed live in prod CloudWatch (2026-09-12):
    # an "ERROR: Exception in ASGI application" / anyio.BrokenResourceError
    # from fastapi/routing.py's internal _keepalive_inserter, raised when a
    # client disconnects (tab closed, switched systems, network drop) at the
    # exact instant that background task is mid-send into its own internal
    # memory stream. That's entirely inside FastAPI's own SSE plumbing, not
    # this function - nothing here catches or causes it, and there's
    # nothing lost for other connected clients (each SSE connection is
    # independent). No ALB/health check hits this endpoint (single EC2,
    # host networking, no target group - see deployment/main.tf) that could
    # be causing artificial disconnect churn, and deployment/alerts.tf only
    # watches ECS task counts/EC2 status checks, not log content, so this
    # can't be what triggers a real alert. Investigated and intentionally
    # left as-is rather than "fixed."
    config = await asyncio.to_thread(get_transit_system_config, transit_system)
    if not config:
        logger.error(f"GTFS URL not found for {transit_system}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    gtfs_url = config["realtime_url"]
    min_poll_interval_seconds = config["min_poll_interval_seconds"]
    quota_group = config["quota_group"]

    posthog_client = getattr(app.state, "posthog_client", None)
    if posthog_client:
        posthog_client.capture(
            "trip_updates_stream_opened",
            distinct_id=None,
            properties={"transit_system": transit_system},
        )

    # Give a newly-connected client something real to show immediately,
    # instead of making them wait out this system's poll/rate-limit cycle
    # (up to min_poll_interval_seconds - e.g. SF-MTA's 240s) for the first
    # live event - see RecentEventsCache.
    # Filtered against whatever's already cached (no new fetch - see
    # RealtimeFeedCache.peek) so a burst entry that's already dropped out
    # of the live feed (its trip ended) isn't served as if it were still
    # live - confirmed live in prod: trip_detail() 404s for a burst-served
    # trip_id no longer in the current feed, which a client has no way to
    # distinguish from a real error. `live_trip_ids is None` (nothing
    # cached yet) means "unknown" - serve the burst unfiltered rather than
    # dropping everything just because we can't yet verify it.
    live_feed = RealtimeFeedCache.peek(transit_system)
    live_trip_ids = (
        {_entity_trip_id(e) for e in live_feed.entity if e.HasField("trip_update")}
        if live_feed is not None
        else None
    )
    for cached_position in RecentEventsCache.get(transit_system):
        if live_trip_ids is not None and cached_position.trip_id not in live_trip_ids:
            continue
        yield ServerSentEvent(data=cached_position, event="trip_update", retry=5000)

    while True:
        (
            trip_headsigns,
            stop_names,
            headsigns_by_route_dir,
            colors_by_trip,
            colors_by_route,
            colors_by_stop,
            short_names_by_trip,
            short_names_by_route,
            long_names_by_trip,
            long_names_by_route,
            route_id_by_trip,
            intra_transfers_by_stop,
        ) = await ScheduleCache.get(transit_system)
        (
            area_ids_by_stop,
            members_by_area,
            systems_by_stop,
            regional_id_by_native_stop,
        ) = await TransferCache.get()
        try:
            # Every connected client runs this same 30s loop independently -
            # without RealtimeFeedCache, N clients would mean N times the
            # outbound request rate to this system's own API. The cache
            # shares one real fetch across all of them, respecting
            # min_poll_interval_seconds (0 - the default - means never
            # cache, so this is a no-op for every system that doesn't need
            # rate limiting) and quota_group, when set, for a budget shared
            # across several different systems (e.g. every 511.org-backed
            # one) - see SharedFeedQuota.
            feed = await RealtimeFeedCache.get(
                transit_system,
                lambda: _fetch_feed(gtfs_url),
                min_poll_interval_seconds,
                quota_group,
            )
        except requests.exceptions.RequestException as ex:
            logger.error(f"Request error fetching feed for {transit_system}: {ex}")
            await asyncio.sleep(30)
            continue
        except Exception as ex:
            logger.error(f"Parse error with Feed Message for {transit_system}: {ex}")
            await asyncio.sleep(30)
            continue

        positions_this_poll: list[TripPosition] = []
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
                # Trip-then-route fallback, same reasoning as route_short_name/
                # route_long_name below - a source that leaves the live
                # entity's own route_id blank (confirmed live: BART) still
                # resolves to the trip's real route_id via stored Schedule
                # data instead of surfacing "" to clients comparing
                # route_short_name against it (see TripPosition.route_id).
                route_id = trip_descriptor.route_id or route_id_by_trip.get(
                    trip_descriptor.trip_id
                )
                # The dict lookups below need a plain str key (a protobuf
                # string field, like trip_descriptor.route_id, is never None
                # - only route_id_by_trip's fallback can be), so this
                # preserves that for the lookups while route_id itself stays
                # Optional for TripPosition.route_id above. "" is a safe
                # placeholder: no route ever has it as a real route_id, so a
                # miss here behaves exactly like the old no-fallback lookup.
                route_id_for_lookup = route_id or ""
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
                    or headsigns_by_route_dir.get((route_id_for_lookup, direction_id))
                    or destination_headsign
                )
                colors = (
                    colors_by_trip.get(trip_descriptor.trip_id)
                    or colors_by_route.get(route_id_for_lookup)
                    or (
                        colors_by_stop.get(destination_stop_id)
                        if destination_stop_id
                        else None
                    )
                )
                color, text_color = colors if colors else (None, None)
                # Same trip-then-route fallback as colors above - a trip's
                # own stored route_id (via short_names_by_trip) covers a
                # source that leaves the live entity's route_id blank (see
                # trip_detail()'s identical reasoning for BART).
                route_short_name = short_names_by_trip.get(
                    trip_descriptor.trip_id
                ) or short_names_by_route.get(route_id_for_lookup)
                route_long_name = long_names_by_trip.get(
                    trip_descriptor.trip_id
                ) or long_names_by_route.get(route_id_for_lookup)
                # trip_update.timestamp ("last measured" time for this
                # vehicle) is the ranking key RecentEventsCache uses to keep
                # its most-recently-updated 50 - not every source sets it
                # per-trip, so fall back to the feed's own header timestamp
                # (always present) rather than leaving it unset.
                timestamp = (
                    entity.trip_update.timestamp
                    if entity.trip_update.HasField("timestamp")
                    else feed.header.timestamp
                )
                # Phase 3 transfers (see .claude/plans/development.md) - same-
                # system routes reachable from here, excluding this trip's own
                # route (transferring to the route you're already on isn't
                # useful info), and other transit systems sharing this stop's
                # physical station complex. Both precomputed (ScheduleCache/
                # TransferCache above), so this is pure in-memory lookup, not
                # a query per entity.
                intra_system_transfers = [
                    transfer
                    for transfer in intra_transfers_by_stop.get(position.stop_id, [])
                    if transfer.route_id != route_id
                ]
                inter_system_transfer_systems = resolve_inter_system_transfers(
                    transit_system,
                    position.stop_id,
                    area_ids_by_stop,
                    members_by_area,
                    systems_by_stop,
                    regional_id_by_native_stop,
                )
                trip_position = TripPosition(
                    trip_id=trip_id,
                    stop_id=position.stop_id,
                    previous=position.previous,
                    next=position.next,
                    status=position.status,
                    trip_headsign=headsign,
                    route_id=route_id,
                    route_short_name=route_short_name,
                    route_long_name=route_long_name,
                    stop_name=stop_names.get(position.stop_id),
                    color=color,
                    text_color=text_color,
                    timestamp=timestamp,
                    intra_system_transfers=intra_system_transfers,
                    inter_system_transfer_systems=inter_system_transfer_systems,
                )
                positions_this_poll.append(trip_position)
                yield ServerSentEvent(
                    data=trip_position,
                    event="trip_update",
                    retry=5000,
                )
        RecentEventsCache.update(transit_system, positions_this_poll)
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
            config["quota_group"],
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

    # Phase 3 transfers (see .claude/plans/development.md) - same shape as
    # transit_feed()'s per-entity lookup, but batched across every stop on
    # this trip in one call rather than one per stop_id.
    (
        area_ids_by_stop,
        members_by_area,
        systems_by_stop,
        regional_id_by_native_stop,
    ) = await TransferCache.get()
    this_trip_route_id = trip_descriptor.route_id or trip_schedule.get("route_id")
    transfers_by_stop = await asyncio.to_thread(
        get_transfers_for_stops,
        transit_system,
        live_stop_ids + tail_stop_ids,
        area_ids_by_stop,
        members_by_area,
        systems_by_stop,
        regional_id_by_native_stop,
    )

    def _intra_transfers(stop_id: str) -> list[IntraSystemTransfer]:
        return [
            transfer
            for transfer in transfers_by_stop.get(stop_id, {}).get("intra", [])
            if transfer.route_id != this_trip_route_id
        ]

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
            intra_system_transfers=_intra_transfers(stu.stop_id),
            inter_system_transfer_systems=transfers_by_stop.get(stu.stop_id, {}).get(
                "inter", []
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
            intra_system_transfers=_intra_transfers(stop_id),
            inter_system_transfer_systems=transfers_by_stop.get(stop_id, {}).get(
                "inter", []
            ),
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
        # against min_poll_interval_seconds, and (via quota_group) against
        # any budget shared with other systems too - e.g. 511.org - without
        # one feed's cache entry clobbering the other's.
        feed = await RealtimeFeedCache.get(
            f"{transit_system}:alerts",
            lambda: _fetch_feed(alerts_url),
            config["min_poll_interval_seconds"],
            config["quota_group"],
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

    schedulers = getattr(app.state, "quota_group_schedulers", [])
    return TransitSystemDetail(
        name=transit_system,
        timezone=config["timezone"],
        default_schedule_url=config["default_schedule_url"],
        auth_required=config["auth_required"],
        next_realtime_update_at=next_realtime_update_at(schedulers, transit_system),
    )


@app.get("/lines/{transit_system}")
async def get_lines(transit_system: str) -> list[LineSummary]:
    """Every distinct (route_id, trip_headsign) pair this system's trips
    use - backs the frontend's Line filter checklist (IRL-26). The live SSE
    buffer alone only ever shows whatever's streamed through the last
    MAX_MESSAGES events, not every line a system actually has. Derived
    entirely from ScheduleCache's existing per-system preload (no new DB
    query) - see LineSummary's own docstring for why trip_headsign rides
    alongside the route fields rather than this just being a route list."""
    config = await asyncio.to_thread(get_transit_system_config, transit_system)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown transit system"
        )

    (
        trips,
        _stops,
        _headsigns_by_route_dir,
        _colors_by_trip,
        _colors_by_route,
        _colors_by_stop,
        _short_names_by_trip,
        short_names_by_route,
        _long_names_by_trip,
        long_names_by_route,
        route_id_by_trip,
        _intra_transfers_by_stop,
    ) = await ScheduleCache.get(transit_system)

    seen: set[tuple[str, str | None]] = set()
    lines: list[LineSummary] = []
    for trip_id, trip_headsign in trips.items():
        route_id = route_id_by_trip.get(trip_id)
        if route_id is None:
            continue
        key = (route_id, trip_headsign)
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            LineSummary(
                route_id=route_id,
                route_short_name=short_names_by_route.get(route_id),
                route_long_name=long_names_by_route.get(route_id),
                trip_headsign=trip_headsign,
            )
        )
    return lines


@app.get("/stops/{transit_system}")
async def get_stops(transit_system: str) -> list[str]:
    """Every distinct stop name this system has - backs the frontend's
    Station filter checklist (IRL-26), same reasoning as get_lines() above.
    Derived from ScheduleCache's existing per-system `stops` preload, no
    new DB query."""
    config = await asyncio.to_thread(get_transit_system_config, transit_system)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown transit system"
        )

    (
        _trips,
        stops,
        _headsigns_by_route_dir,
        _colors_by_trip,
        _colors_by_route,
        _colors_by_stop,
        _short_names_by_trip,
        _short_names_by_route,
        _long_names_by_trip,
        _long_names_by_route,
        _route_id_by_trip,
        _intra_transfers_by_stop,
    ) = await ScheduleCache.get(transit_system)
    return sorted(set(stops.values()))


@app.get("/info")
async def info() -> dict[str, Any]:
    settings = get_settings()
    return {
        "app_name": settings.app_name,
        "admin_email": settings.admin_email,
        "env": settings.env,
        "debug_mode": settings.debug,
    }
