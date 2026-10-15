from generated import gtfs_realtime_pb2
from src.services.regional_feed import (
    compare_alert_feeds,
    compare_feeds,
    split_feed_by_agency,
)

AGENCY_MAP = {"SF": "SF-MTA", "AC": "AC_Transit"}


def _plain_feed() -> gtfs_realtime_pb2.FeedMessage:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    return feed


def _add_trip_update(
    feed: gtfs_realtime_pb2.FeedMessage,
    entity_id: str,
    trip_id: str,
    route_id: str,
    stop_ids: list[str],
) -> None:
    entity = feed.entity.add()
    entity.id = entity_id
    entity.trip_update.trip.trip_id = trip_id
    entity.trip_update.trip.route_id = route_id
    for stop_id in stop_ids:
        entity.trip_update.stop_time_update.add().stop_id = stop_id


def _rg_feed() -> gtfs_realtime_pb2.FeedMessage:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = 1700000000
    return feed


def test_splits_entities_by_agency_code_prefix() -> None:
    feed = _rg_feed()

    sf_entity = feed.entity.add()
    sf_entity.id = "SF:e1"
    sf_entity.trip_update.trip.trip_id = "SF:T1"
    sf_entity.trip_update.trip.route_id = "SF:R1"

    ac_entity = feed.entity.add()
    ac_entity.id = "AC:e2"
    ac_entity.trip_update.trip.trip_id = "AC:T2"
    ac_entity.trip_update.trip.route_id = "AC:R2"

    messages = split_feed_by_agency(feed, AGENCY_MAP)

    assert set(messages.keys()) == {"SF-MTA", "AC_Transit"}
    assert len(messages["SF-MTA"].entity) == 1
    assert len(messages["AC_Transit"].entity) == 1


def test_strips_agency_prefix_from_trip_update_fields() -> None:
    feed = _rg_feed()

    entity = feed.entity.add()
    entity.id = "SF:e1"
    entity.trip_update.trip.trip_id = "SF:12133132_M11"
    entity.trip_update.trip.route_id = "SF:L"
    entity.trip_update.vehicle.id = "SF:v1"
    stop_time_update = entity.trip_update.stop_time_update.add()
    stop_time_update.stop_id = "SF:4015"

    [stripped] = split_feed_by_agency(feed, AGENCY_MAP)["SF-MTA"].entity

    assert stripped.id == "e1"
    assert stripped.trip_update.trip.trip_id == "12133132_M11"
    assert stripped.trip_update.trip.route_id == "L"
    assert stripped.trip_update.vehicle.id == "v1"
    assert stripped.trip_update.stop_time_update[0].stop_id == "4015"


def test_alerts_are_attributed_by_agency_id_not_a_prefix() -> None:
    # Confirmed live (AC Transit, IRL-17): alerts don't use the
    # <AGENCY-CODE>: prefix convention TripUpdates do at all -
    # route_id/trip_id/stop_id and entity.id are the owning agency's own
    # raw, unprefixed values (AC Transit's own native alert IDs happen to
    # start with "AC_", that agency's own internal convention, unrelated
    # to 511.org's demux). informed_entity.agency_id is the real signal.
    feed = _rg_feed()

    entity = feed.entity.add()
    entity.id = "AC_14th Ave. at E. 33rd St. Stop Closed"
    informed = entity.alert.informed_entity.add()
    informed.stop_id = "55202"
    informed.agency_id = "AC"

    messages = split_feed_by_agency(feed, AGENCY_MAP)

    assert "AC_Transit" in messages
    [result] = messages["AC_Transit"].entity
    # Nothing is prefixed, so nothing should be stripped - the raw values
    # pass through unchanged.
    assert result.id == "AC_14th Ave. at E. 33rd St. Stop Closed"
    assert result.alert.informed_entity[0].stop_id == "55202"


def test_alert_with_only_stop_id_and_agency_id_is_not_dropped() -> None:
    # The exact failure mode this fix addresses: before it, an alert whose
    # informed_entity has no route_id/trip_id (common for a "stop closed"
    # alert) and whose entity.id isn't colon-prefixed fell through every
    # attribution path and was silently skipped entirely - not just
    # mis-attributed.
    feed = _rg_feed()

    entity = feed.entity.add()
    entity.id = "SomeNativeAlertId"
    informed = entity.alert.informed_entity.add()
    informed.agency_id = "SF"
    informed.stop_id = "4015"

    messages = split_feed_by_agency(feed, AGENCY_MAP)

    assert "SF-MTA" in messages
    assert len(messages["SF-MTA"].entity) == 1


def test_agency_code_with_no_mapping_entry_is_skipped() -> None:
    feed = _rg_feed()

    entity = feed.entity.add()
    entity.id = "GG:e1"
    entity.trip_update.trip.trip_id = "GG:T1"
    entity.trip_update.trip.route_id = "GG:R1"

    messages = split_feed_by_agency(feed, AGENCY_MAP)

    assert messages == {}


def test_falls_back_to_entity_id_when_trip_fields_are_blank() -> None:
    # Mirrors a source like HSL that never sets trip_id - see
    # main.py's _entity_trip_id - but still needs *some* signal to
    # attribute the entity to an agency.
    feed = _rg_feed()

    entity = feed.entity.add()
    entity.id = "AC:e1"

    messages = split_feed_by_agency(feed, AGENCY_MAP)

    assert "AC_Transit" in messages
    assert messages["AC_Transit"].entity[0].id == "e1"


def test_entity_with_no_prefixed_field_anywhere_is_skipped() -> None:
    feed = _rg_feed()

    entity = feed.entity.add()
    entity.id = "unprefixed-id"
    entity.trip_update.trip.trip_id = "unprefixed-trip"

    messages = split_feed_by_agency(feed, AGENCY_MAP)

    assert messages == {}


def test_result_messages_copy_the_shared_feed_header() -> None:
    feed = _rg_feed()
    entity = feed.entity.add()
    entity.id = "SF:e1"
    entity.trip_update.trip.trip_id = "SF:T1"
    entity.trip_update.trip.route_id = "SF:R1"

    messages = split_feed_by_agency(feed, AGENCY_MAP)

    assert messages["SF-MTA"].header.timestamp == feed.header.timestamp
    assert messages["SF-MTA"].header.gtfs_realtime_version == "2.0"


def test_original_feed_is_not_mutated() -> None:
    feed = _rg_feed()
    entity = feed.entity.add()
    entity.id = "SF:e1"
    entity.trip_update.trip.trip_id = "SF:T1"
    entity.trip_update.trip.route_id = "SF:R1"

    split_feed_by_agency(feed, AGENCY_MAP)

    assert feed.entity[0].trip_update.trip.trip_id == "SF:T1"
    assert feed.entity[0].trip_update.trip.route_id == "SF:R1"


def test_compare_feeds_reports_no_diff_when_identical() -> None:
    direct = _plain_feed()
    _add_trip_update(direct, "e1", "T1", "R1", ["4015", "4016"])

    demuxed = _plain_feed()
    _add_trip_update(demuxed, "e1", "T1", "R1", ["4015", "4016"])

    report = compare_feeds(direct, demuxed)

    assert report["direct_entity_count"] == 1
    assert report["demuxed_entity_count"] == 1
    assert report["matched_entity_count"] == 1
    assert report["entity_ids_only_in_direct"] == []
    assert report["entity_ids_only_in_demuxed"] == []
    assert report["route_ids_only_in_direct"] == []
    assert report["route_ids_only_in_demuxed"] == []
    assert report["stop_ids_only_in_direct"] == []
    assert report["stop_ids_only_in_demuxed"] == []
    assert report["matched_trip_stop_id_mismatches"] == {}


def test_compare_feeds_surfaces_unstripped_stop_id_prefix() -> None:
    # Exactly the failure mode IRL-15 exists to catch: if stop_id isn't
    # actually prefixed the same way route_id/trip_id are (or the demux
    # fails to strip it), the "demuxed" side's stop_ids won't match the
    # direct fetch's real stop_ids at all.
    direct = _plain_feed()
    _add_trip_update(direct, "e1", "T1", "R1", ["4015"])

    demuxed = _plain_feed()
    _add_trip_update(demuxed, "e1", "T1", "R1", ["SF:4015"])

    report = compare_feeds(direct, demuxed)

    assert report["stop_ids_only_in_direct"] == ["4015"]
    assert report["stop_ids_only_in_demuxed"] == ["SF:4015"]
    # The decisive signal: entity "e1" exists on both sides (not churn -
    # same trip), and its stop sequences genuinely differ.
    assert report["matched_trip_stop_id_mismatches"] == {
        "e1": {"direct": ["4015"], "demuxed": ["SF:4015"]}
    }


def test_compare_feeds_matched_trips_are_clean_despite_unmatched_churn() -> None:
    # Mirrors what a live comparison actually looks like: the two fetches
    # aren't simultaneous, so a trip can legitimately appear on only one
    # side (see e2/e3 below) without that being a stop_id-handling bug -
    # matched_trip_stop_id_mismatches must stay empty as long as every
    # trip present on *both* sides has identical stop sequences, even
    # though the flat stop_ids_only_in_* sets are non-empty here.
    direct = _plain_feed()
    _add_trip_update(direct, "e1", "T1", "R1", ["4015", "4016"])
    _add_trip_update(direct, "e2", "T2", "R2", ["5000"])  # churned out by the 2nd fetch

    demuxed = _plain_feed()
    _add_trip_update(demuxed, "e1", "T1", "R1", ["4015", "4016"])
    _add_trip_update(demuxed, "e3", "T3", "R3", ["6000"])  # appeared only after churn

    report = compare_feeds(direct, demuxed)

    assert report["stop_ids_only_in_direct"] == ["5000"]
    assert report["stop_ids_only_in_demuxed"] == ["6000"]
    assert report["matched_trip_stop_id_mismatches"] == {}


def test_compare_feeds_surfaces_missing_entity() -> None:
    direct = _plain_feed()
    _add_trip_update(direct, "e1", "T1", "R1", ["4015"])
    _add_trip_update(direct, "e2", "T2", "R2", ["4016"])

    demuxed = _plain_feed()
    _add_trip_update(demuxed, "e1", "T1", "R1", ["4015"])

    report = compare_feeds(direct, demuxed)

    assert report["entity_ids_only_in_direct"] == ["e2"]
    assert report["entity_ids_only_in_demuxed"] == []
    assert report["route_ids_only_in_direct"] == ["R2"]


def test_compare_feeds_surfaces_route_id_mismatch() -> None:
    direct = _plain_feed()
    _add_trip_update(direct, "e1", "T1", "R1", ["4015"])

    demuxed = _plain_feed()
    _add_trip_update(demuxed, "e1", "T1", "SF:R1", ["4015"])

    report = compare_feeds(direct, demuxed)

    assert report["route_ids_only_in_direct"] == ["R1"]
    assert report["route_ids_only_in_demuxed"] == ["SF:R1"]


def test_compare_feeds_ignores_non_trip_update_entities() -> None:
    direct = _plain_feed()
    _add_trip_update(direct, "e1", "T1", "R1", ["4015"])
    alert_only = direct.entity.add()
    alert_only.id = "alert1"
    alert_only.alert.header_text.translation.add().text = "Delay"

    demuxed = _plain_feed()
    _add_trip_update(demuxed, "e1", "T1", "R1", ["4015"])

    report = compare_feeds(direct, demuxed)

    assert report["direct_entity_count"] == 1
    assert report["entity_ids_only_in_direct"] == []


def _add_alert(
    feed: gtfs_realtime_pb2.FeedMessage,
    entity_id: str,
    route_id: str,
    trip_id: str,
) -> None:
    entity = feed.entity.add()
    entity.id = entity_id
    informed = entity.alert.informed_entity.add()
    informed.route_id = route_id
    informed.trip.trip_id = trip_id


def test_compare_alert_feeds_reports_no_mismatch_when_identical() -> None:
    direct = _plain_feed()
    _add_alert(direct, "a1", "R1", "T1")

    demuxed = _plain_feed()
    _add_alert(demuxed, "a1", "R1", "T1")

    report = compare_alert_feeds(direct, demuxed)

    assert report["direct_entity_count"] == 1
    assert report["matched_entity_count"] == 1
    assert report["entity_ids_only_in_direct"] == []
    assert report["entity_ids_only_in_demuxed"] == []
    assert report["matched_informed_entity_mismatches"] == {}


def test_compare_alert_feeds_surfaces_unstripped_informed_entity_prefix() -> None:
    direct = _plain_feed()
    _add_alert(direct, "a1", "R1", "T1")

    demuxed = _plain_feed()
    _add_alert(demuxed, "a1", "SF:R1", "SF:T1")

    report = compare_alert_feeds(direct, demuxed)

    assert report["matched_informed_entity_mismatches"] == {
        "a1": {"direct": [("R1", "T1")], "demuxed": [("SF:R1", "SF:T1")]}
    }


def test_compare_alert_feeds_ignores_unmatched_churn() -> None:
    direct = _plain_feed()
    _add_alert(direct, "a1", "R1", "T1")
    _add_alert(direct, "a2", "R2", "T2")  # only in direct

    demuxed = _plain_feed()
    _add_alert(demuxed, "a1", "R1", "T1")
    _add_alert(demuxed, "a3", "R3", "T3")  # only in demuxed

    report = compare_alert_feeds(direct, demuxed)

    assert report["entity_ids_only_in_direct"] == ["a2"]
    assert report["entity_ids_only_in_demuxed"] == ["a3"]
    assert report["matched_informed_entity_mismatches"] == {}
