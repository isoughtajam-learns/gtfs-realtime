"""
Splits a combined `agency=RG` GTFS-Realtime FeedMessage (511.org's
region-wide Bay Area feed) into one FeedMessage per transit_system, by the
`<AGENCY-CODE>:` prefix 511.org puts on entity ID fields - confirmed live
for TripUpdates' trip_update.trip.route_id/trip_id (see
docs/rate-limiting-and-scheduling.md's "Confirmed: a single request covers
every Bay Area agency"). 511.org has separately confirmed ServiceAlerts
also supports agency=RG, but the exact field-level prefixing there hasn't
been checked against live data yet (see IRL-15) - this module strips
alerts' informed_entity fields on the same assumption, which IRL-15 is
responsible for confirming (or correcting) before anything depends on it
for real traffic.

Only strips the ID fields this app actually reads downstream - see
main.py's transit_feed()/trip_detail() and service_alerts.py - not every
field the GTFS-RT spec defines. Nothing here reads VehiclePositions-only
entities, shapes, stops, or trip_modifications, so those aren't handled.
"""

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from generated import gtfs_realtime_pb2


def split_feed_by_agency(
    feed: gtfs_realtime_pb2.FeedMessage,
    agency_code_to_transit_system: Dict[str, str],
) -> Dict[str, gtfs_realtime_pb2.FeedMessage]:
    """Returns {transit_system_name: FeedMessage} for every agency present
    in `feed` that's also a key (by 511.org agency code - see IRL-7's
    findings) in `agency_code_to_transit_system`. An agency code appearing
    in the feed with no matching entry is skipped entirely - not every one
    of 511.org's ~40 operators is a system this app serves - rather than
    raising or attributing it to the wrong system.

    Every returned FeedMessage shares the same feed.header as the input:
    agency=RG's response has one header for the whole combined feed, not
    one per agency, so there's nothing more specific to copy per system."""
    entities_by_system: Dict[str, List[gtfs_realtime_pb2.FeedEntity]] = defaultdict(
        list
    )

    for entity in feed.entity:
        agency_code = _entity_agency_code(entity)
        if agency_code is None:
            continue
        transit_system = agency_code_to_transit_system.get(agency_code)
        if transit_system is None:
            continue
        entities_by_system[transit_system].append(_stripped_entity(entity, agency_code))

    messages: Dict[str, gtfs_realtime_pb2.FeedMessage] = {}
    for transit_system, entities in entities_by_system.items():
        message = gtfs_realtime_pb2.FeedMessage()
        message.header.CopyFrom(feed.header)
        message.entity.extend(entities)
        messages[transit_system] = message
    return messages


def _agency_code_and_id(prefixed: str) -> Tuple[Optional[str], str]:
    """Splits "<AGENCY-CODE>:<original-id>" into (agency_code, original_id).
    Returns (None, prefixed) unchanged if there's no ':' - a blank or
    already-unprefixed value is passed through as-is rather than raising."""
    agency_code, sep, original_id = prefixed.partition(":")
    if not sep:
        return None, prefixed
    return agency_code, original_id


def _entity_agency_code(entity: gtfs_realtime_pb2.FeedEntity) -> Optional[str]:
    """Which 511.org agency code this entity belongs to, read from
    whichever confirmed-prefixed field is present: trip_update.trip's
    route_id/trip_id for TripUpdates (the only fields the original
    agency=RG investigation actually verified are prefixed), or the first
    informed_entity's equivalents for ServiceAlerts (assumed pending
    IRL-15's live check). Falls back to entity.id itself - also
    unconfirmed - only if neither trip field is usable, e.g. a source that
    leaves both blank the way HSL does for trip_id alone (see
    main.py's _entity_trip_id)."""
    if entity.HasField("trip_update"):
        trip = entity.trip_update.trip
        for value in (trip.route_id, trip.trip_id):
            code, _ = _agency_code_and_id(value)
            if code:
                return code

    if entity.HasField("alert") and entity.alert.informed_entity:
        informed = entity.alert.informed_entity[0]
        for value in (informed.route_id, informed.trip.trip_id):
            code, _ = _agency_code_and_id(value)
            if code:
                return code

    code, _ = _agency_code_and_id(entity.id)
    return code


def _stripped_entity(
    entity: gtfs_realtime_pb2.FeedEntity, agency_code: str
) -> gtfs_realtime_pb2.FeedEntity:
    """A copy of `entity` with the "<AGENCY-CODE>:" prefix removed from
    every ID field this app reads downstream, so demuxed entities carry
    exactly the same ID format this app's existing per-agency ingestion
    and stored Schedule data already use."""
    stripped = gtfs_realtime_pb2.FeedEntity()
    stripped.CopyFrom(entity)

    prefix = f"{agency_code}:"

    def _strip(value: str) -> str:
        return value[len(prefix) :] if value.startswith(prefix) else value

    stripped.id = _strip(stripped.id)

    if stripped.HasField("trip_update"):
        trip_update = stripped.trip_update
        trip_update.trip.trip_id = _strip(trip_update.trip.trip_id)
        trip_update.trip.route_id = _strip(trip_update.trip.route_id)
        if trip_update.HasField("vehicle"):
            trip_update.vehicle.id = _strip(trip_update.vehicle.id)
        for stop_time_update in trip_update.stop_time_update:
            stop_time_update.stop_id = _strip(stop_time_update.stop_id)

    if stripped.HasField("alert"):
        for informed in stripped.alert.informed_entity:
            informed.route_id = _strip(informed.route_id)
            informed.stop_id = _strip(informed.stop_id)
            if informed.HasField("trip"):
                informed.trip.trip_id = _strip(informed.trip.trip_id)
                informed.trip.route_id = _strip(informed.trip.route_id)

    return stripped
