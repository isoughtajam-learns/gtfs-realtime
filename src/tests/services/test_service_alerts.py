from generated import gtfs_realtime_pb2
from src.services.service_alerts import (
    _translated_text,
    build_service_alerts,
    extract_referenced_ids,
)


def _feed_with_alert(
    *,
    entity_id: str = "A1",
    trip_id: str = "",
    route_id: str = "",
    include_stop_only_entity: bool = False,
) -> gtfs_realtime_pb2.FeedMessage:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    entity = feed.entity.add()
    entity.id = entity_id
    alert = entity.alert
    alert.cause = gtfs_realtime_pb2.Alert.MAINTENANCE
    alert.effect = gtfs_realtime_pb2.Alert.DETOUR
    alert.header_text.translation.add(text="Weekend maintenance", language="en")
    alert.description_text.translation.add(text="Shuttle buses", language="en")
    period = alert.active_period.add()
    period.start = 1_700_000_000
    period.end = 1_700_100_000
    if trip_id:
        informed = alert.informed_entity.add()
        informed.trip.trip_id = trip_id
    if route_id:
        informed = alert.informed_entity.add()
        informed.route_id = route_id
    if include_stop_only_entity:
        # A GTFS-RT alert can name only a stop_id, with no trip or route on
        # that same informed_entity - extract_referenced_ids should just
        # ignore it (v1 scope is trips/routes only, see issue #16), not
        # error on the missing fields.
        informed = alert.informed_entity.add()
        informed.stop_id = "S1"
    return feed


def test_translated_text_returns_first_translation() -> None:
    translated = gtfs_realtime_pb2.TranslatedString()
    translated.translation.add(text="English text", language="en")
    translated.translation.add(text="Texto en espanol", language="es")

    assert _translated_text(translated) == "English text"


def test_translated_text_returns_none_when_empty() -> None:
    assert _translated_text(gtfs_realtime_pb2.TranslatedString()) is None


def test_extract_referenced_ids_collects_trip_and_route_ids() -> None:
    feed = _feed_with_alert(trip_id="T1", route_id="R1")

    trip_ids, route_ids = extract_referenced_ids(feed)

    assert trip_ids == {"T1"}
    assert route_ids == {"R1"}


def test_extract_referenced_ids_ignores_stop_only_entities() -> None:
    feed = _feed_with_alert(include_stop_only_entity=True)

    trip_ids, route_ids = extract_referenced_ids(feed)

    assert trip_ids == set()
    assert route_ids == set()


def test_extract_referenced_ids_ignores_non_alert_entities() -> None:
    feed = gtfs_realtime_pb2.FeedMessage()
    entity = feed.entity.add()
    entity.id = "e1"
    entity.trip_update.trip.trip_id = "T1"

    trip_ids, route_ids = extract_referenced_ids(feed)

    assert trip_ids == set()
    assert route_ids == set()


def test_extract_referenced_ids_empty_feed() -> None:
    assert extract_referenced_ids(gtfs_realtime_pb2.FeedMessage()) == (set(), set())


def test_build_service_alerts_hydrates_trip_and_route() -> None:
    feed = _feed_with_alert(trip_id="T1")

    alerts = build_service_alerts(
        feed,
        trips_by_id={"T1": {"trip_headsign": "Downtown", "route_id": "R1"}},
        routes_by_id={"R1": {"route_short_name": "Red", "route_long_name": "Red Line"}},
    )

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["alert_id"] == "A1"
    assert alert["cause"] == "MAINTENANCE"
    assert alert["effect"] == "DETOUR"
    assert alert["severity_level"] is None
    assert alert["header_text"] == "Weekend maintenance"
    assert alert["description_text"] == "Shuttle buses"
    assert alert["active_period"] == [{"start": 1_700_000_000, "end": 1_700_100_000}]
    assert alert["affected_trips"] == [
        {"trip_id": "T1", "trip_headsign": "Downtown", "route_id": "R1"}
    ]
    # R1 was only reachable through T1's own route_id, not named directly
    # in any informed_entity - still expected to be pulled in.
    assert alert["affected_routes"] == [
        {"route_id": "R1", "route_short_name": "Red", "route_long_name": "Red Line"}
    ]


def test_build_service_alerts_directly_named_route_without_trip() -> None:
    feed = _feed_with_alert(route_id="R1")

    alerts = build_service_alerts(
        feed,
        trips_by_id={},
        routes_by_id={"R1": {"route_short_name": "Red", "route_long_name": "Red Line"}},
    )

    assert alerts[0]["affected_trips"] == []
    assert alerts[0]["affected_routes"] == [
        {"route_id": "R1", "route_short_name": "Red", "route_long_name": "Red Line"}
    ]


def test_build_service_alerts_degrades_gracefully_without_hydration_match() -> None:
    """A referenced trip/route with no match in our stored Schedule data
    should still show up, just with the hydrated fields as None - not get
    dropped."""
    feed = _feed_with_alert(trip_id="T1")

    alerts = build_service_alerts(feed, trips_by_id={}, routes_by_id={})

    assert alerts[0]["affected_trips"] == [
        {"trip_id": "T1", "trip_headsign": None, "route_id": None}
    ]
    assert alerts[0]["affected_routes"] == []


def test_build_service_alerts_ignores_non_alert_entities() -> None:
    feed = gtfs_realtime_pb2.FeedMessage()
    entity = feed.entity.add()
    entity.id = "e1"
    entity.trip_update.trip.trip_id = "T1"

    assert build_service_alerts(feed, {}, {}) == []


def test_build_service_alerts_severity_level_unset_is_none() -> None:
    feed = _feed_with_alert()

    alerts = build_service_alerts(feed, {}, {})

    assert alerts[0]["severity_level"] is None


def test_build_service_alerts_severity_level_set() -> None:
    feed = gtfs_realtime_pb2.FeedMessage()
    entity = feed.entity.add()
    entity.id = "A1"
    entity.alert.cause = gtfs_realtime_pb2.Alert.MAINTENANCE
    entity.alert.effect = gtfs_realtime_pb2.Alert.DETOUR
    entity.alert.severity_level = gtfs_realtime_pb2.Alert.WARNING

    alerts = build_service_alerts(feed, {}, {})

    assert alerts[0]["severity_level"] == "WARNING"
