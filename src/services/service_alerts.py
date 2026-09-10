"""
GTFS-RT ServiceAlerts backing GET /service_alerts/{transit_system}: a
distinct feed from realtime_url (TripUpdates) - see
models.TransitSystem.alerts_url. Follows trip_detail.py's split between a
DB-touching hydration lookup and a pure assembly function, so the assembly
logic (which entities got matched to which alert, how active_period/
cause/effect get shaped) is directly unit-testable without a database.
"""

from typing import Any, Optional

from sqlalchemy import select

from generated import gtfs_realtime_pb2
from src.database import engine
from src.models import Route, Trip, TransitSystem


def _translated_text(translated: gtfs_realtime_pb2.TranslatedString) -> Optional[str]:
    """GTFS-RT text fields are a list of (text, language) translations, not
    a bare string - takes the first one. Real feeds observed so far (BART,
    SF-MTA) only ever publish a single, English translation; picking the
    first is a reasonable simplification rather than exposing every
    consumer to the full translation-list shape for no current benefit."""
    return translated.translation[0].text if translated.translation else None


def extract_referenced_ids(
    feed: gtfs_realtime_pb2.FeedMessage,
) -> tuple[set[str], set[str]]:
    """Scans every Alert entity's informed_entity list for trip_id/route_id
    references, so the caller can hydrate all of them with a single batched
    DB lookup per alerts fetch instead of one query per entity. Returns
    (trip_ids, route_ids) - route_ids only includes ones referenced
    *directly* (informed_entity.route_id); a trip's own route_id is folded
    in later by get_alert_hydration_data, once we know which trips exist in
    our stored Schedule data."""
    trip_ids: set[str] = set()
    route_ids: set[str] = set()
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        for informed in entity.alert.informed_entity:
            if informed.route_id:
                route_ids.add(informed.route_id)
            if informed.HasField("trip") and informed.trip.trip_id:
                trip_ids.add(informed.trip.trip_id)
    return trip_ids, route_ids


def get_alert_hydration_data(
    transit_system: str, trip_ids: list[str], route_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Batched lookup for hydrating service alerts - one query for every
    referenced trip, one for every referenced route (plus any route only
    referenced *through* one of those trips), rather than a query per
    alert/entity. A trip_id or route_id not found in our stored Schedule
    data is simply absent from the returned dict - build_service_alerts
    degrades to just the bare id in that case, same pattern as
    trip_detail.py. Returns (trips_by_id, routes_by_id)."""
    with engine.begin() as connection:
        transit_system_id = connection.execute(
            select(TransitSystem.id).where(TransitSystem.name == transit_system)
        ).scalar()
        if transit_system_id is None:
            return {}, {}

        trips_by_id: dict[str, dict[str, Any]] = {}
        if trip_ids:
            trip_rows = connection.execute(
                select(Trip.trip_id, Trip.name, Trip.route_id).where(
                    Trip.transit_system_id == transit_system_id,
                    Trip.trip_id.in_(trip_ids),
                )
            ).all()
            trips_by_id = {
                row.trip_id: {"trip_headsign": row.name, "route_id": row.route_id}
                for row in trip_rows
            }

        # A trip can be affected via a route not itself named in any
        # informed_entity (only reachable through the trip's own route_id) -
        # fold those in before the route lookup, so affected_trips[].route_id
        # always resolves to a real route_short_name/long_name when we have one.
        all_route_ids = set(route_ids) | {
            trip["route_id"] for trip in trips_by_id.values() if trip["route_id"]
        }
        routes_by_id: dict[str, dict[str, Any]] = {}
        if all_route_ids:
            route_rows = connection.execute(
                select(Route.route_id, Route.short_name, Route.long_name).where(
                    Route.transit_system_id == transit_system_id,
                    Route.route_id.in_(all_route_ids),
                )
            ).all()
            routes_by_id = {
                row.route_id: {
                    "route_short_name": row.short_name,
                    "route_long_name": row.long_name,
                }
                for row in route_rows
            }
        return trips_by_id, routes_by_id


def build_service_alerts(
    feed: gtfs_realtime_pb2.FeedMessage,
    trips_by_id: dict[str, dict[str, Any]],
    routes_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pure assembly: turns a parsed feed's Alert entities into
    ServiceAlert-shaped dicts, given the hydration lookups
    get_alert_hydration_data already ran. Split out so this - the part
    with actual logic (matching entities to alerts, cause/effect naming,
    active_period shaping) - is unit-testable without a database."""
    alerts = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert

        trip_ids: set[str] = set()
        route_ids: set[str] = set()
        for informed in alert.informed_entity:
            if informed.route_id:
                route_ids.add(informed.route_id)
            if informed.HasField("trip") and informed.trip.trip_id:
                trip_ids.add(informed.trip.trip_id)

        affected_trips = [
            {
                "trip_id": trip_id,
                "trip_headsign": trips_by_id.get(trip_id, {}).get("trip_headsign"),
                "route_id": trips_by_id.get(trip_id, {}).get("route_id"),
            }
            for trip_id in sorted(trip_ids)
        ]
        # A trip's own route_id (from our stored Schedule data, not the
        # possibly-blank one on the live entity) counts as an affected
        # route too, even if informed_entity never named it directly.
        all_route_ids = route_ids | {
            t["route_id"] for t in affected_trips if t["route_id"]
        }
        affected_routes = [
            {
                "route_id": route_id,
                "route_short_name": routes_by_id.get(route_id, {}).get(
                    "route_short_name"
                ),
                "route_long_name": routes_by_id.get(route_id, {}).get(
                    "route_long_name"
                ),
            }
            for route_id in sorted(all_route_ids)
        ]

        alerts.append(
            {
                "alert_id": entity.id,
                "cause": gtfs_realtime_pb2.Alert.Cause.Name(alert.cause),
                "effect": gtfs_realtime_pb2.Alert.Effect.Name(alert.effect),
                "severity_level": (
                    gtfs_realtime_pb2.Alert.SeverityLevel.Name(alert.severity_level)
                    if alert.HasField("severity_level")
                    else None
                ),
                "header_text": _translated_text(alert.header_text),
                "description_text": _translated_text(alert.description_text),
                "url": _translated_text(alert.url),
                "active_period": [
                    {
                        "start": period.start if period.HasField("start") else None,
                        "end": period.end if period.HasField("end") else None,
                    }
                    for period in alert.active_period
                ],
                "affected_trips": affected_trips,
                "affected_routes": affected_routes,
            }
        )
    return alerts
