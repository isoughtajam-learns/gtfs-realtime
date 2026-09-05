from src.services.schedule_utils import (
    dedupe_rows_by_columns,
    is_earlier_stop_sequence,
    is_later_stop_sequence,
    missing_route_fields,
    missing_stop_fields,
    parse_optional_float,
    parse_optional_int,
    resolve_route_url,
    resolve_trip_headsign,
)


def test_resolve_trip_headsign_prefers_destination_stop_name() -> None:
    assert (
        resolve_trip_headsign("Destination", "Direct", "ViaStopTime", "ViaRoute")
        == "Destination"
    )


def test_resolve_trip_headsign_falls_back_to_direct_value() -> None:
    # Real-world case: BART's Saturday through-service trips carry a
    # trip_headsign copied from an unrelated weekday route pattern - the
    # trip's own destination stop name (checked first, above) is what
    # catches that; trips.txt's raw value is only trusted when we have no
    # Schedule stop data at all for this trip.
    assert resolve_trip_headsign(None, "Direct", "ViaStopTime", "ViaRoute") == "Direct"
    assert resolve_trip_headsign("", "Direct", "ViaStopTime", "ViaRoute") == "Direct"


def test_resolve_trip_headsign_falls_back_to_stop_time() -> None:
    assert resolve_trip_headsign(None, None, "ViaStopTime", "ViaRoute") == "ViaStopTime"
    assert resolve_trip_headsign(None, "", "ViaStopTime", "ViaRoute") == "ViaStopTime"


def test_resolve_trip_headsign_falls_back_to_route_long_name() -> None:
    assert resolve_trip_headsign(None, None, None, "ViaRoute") == "ViaRoute"


def test_resolve_trip_headsign_none_when_all_sources_empty() -> None:
    assert resolve_trip_headsign(None, None, None, None) is None
    assert resolve_trip_headsign("", "", "", "") is None


def test_is_earlier_stop_sequence_first_candidate_always_wins() -> None:
    assert is_earlier_stop_sequence(5, None) is True


def test_is_earlier_stop_sequence_lower_number_wins() -> None:
    assert is_earlier_stop_sequence(2, 5) is True
    assert is_earlier_stop_sequence(5, 2) is False


def test_is_earlier_stop_sequence_none_candidate_never_wins() -> None:
    assert is_earlier_stop_sequence(None, 5) is False
    assert is_earlier_stop_sequence(None, None) is False


def test_is_later_stop_sequence_first_candidate_always_wins() -> None:
    assert is_later_stop_sequence(5, None) is True


def test_is_later_stop_sequence_higher_number_wins() -> None:
    assert is_later_stop_sequence(5, 2) is True
    assert is_later_stop_sequence(2, 5) is False


def test_is_later_stop_sequence_none_candidate_never_wins() -> None:
    assert is_later_stop_sequence(None, 5) is False
    assert is_later_stop_sequence(None, None) is False


def test_resolve_route_url_prefers_own_url() -> None:
    assert (
        resolve_route_url(
            "https://agency.example/route/1", "https://agency.example/schedules"
        )
        == "https://agency.example/route/1"
    )


def test_resolve_route_url_falls_back_to_default() -> None:
    assert (
        resolve_route_url(None, "https://agency.example/schedules")
        == "https://agency.example/schedules"
    )
    assert (
        resolve_route_url("", "https://agency.example/schedules")
        == "https://agency.example/schedules"
    )


def test_resolve_route_url_none_when_no_default_configured() -> None:
    assert resolve_route_url(None, None) is None


def test_parse_optional_int_valid_values() -> None:
    assert parse_optional_int("0") == 0
    assert parse_optional_int("1") == 1


def test_parse_optional_int_blank_or_malformed_is_none() -> None:
    assert parse_optional_int(None) is None
    assert parse_optional_int("") is None
    assert parse_optional_int("not-a-number") is None


def test_parse_optional_float_valid_values() -> None:
    assert parse_optional_float("37.7749") == 37.7749
    assert parse_optional_float("-122.4194") == -122.4194


def test_parse_optional_float_blank_or_malformed_is_none() -> None:
    assert parse_optional_float(None) is None
    assert parse_optional_float("") is None
    assert parse_optional_float("not-a-number") is None


def test_missing_route_fields_empty_when_all_present() -> None:
    assert (
        missing_route_fields("R1", "1", "First Ave", "https://x", "FF0000", "FFFFFF")
        == []
    )


def test_missing_route_fields_reports_each_missing_field() -> None:
    missing = missing_route_fields("R1", None, "First Ave", "https://x", None, "FFFFFF")
    assert missing == ["route_short_name", "route_color"]


def test_missing_route_fields_hsl_style_all_colors_missing() -> None:
    """Real-world case: HSL's routes.txt has no route_color/route_text_color
    columns at all, so every route is missing both."""
    missing = missing_route_fields(
        "1001", "1", "Eira - Lasipalatsi", "https://x", None, None
    )
    assert missing == ["route_color", "route_text_color"]


def test_missing_stop_fields_empty_when_all_present() -> None:
    assert missing_stop_fields("T1", "Union City") == []


def test_missing_stop_fields_reports_each_missing_field() -> None:
    missing = missing_stop_fields(None, "Union City")
    assert missing == ["trip_id (via stop_times.txt)"]


def test_missing_stop_fields_kiev_style_zone_id_absent_is_still_usable() -> None:
    """Real-world case: Kiev's stops.txt has no zone_id column at all, but
    the stop is still usable - zone_id isn't a required field."""
    assert missing_stop_fields("T1", "ТРЕД № 1") == []


def test_dedupe_rows_by_columns_no_duplicates_returns_all_rows() -> None:
    rows = [{"id": "A", "v": 1}, {"id": "B", "v": 2}]
    assert dedupe_rows_by_columns(rows, ["id"]) == rows


def test_dedupe_rows_by_columns_keeps_last_occurrence() -> None:
    rows = [
        {"id": "A", "v": 1},
        {"id": "A", "v": 2},
        {"id": "B", "v": 3},
    ]
    assert dedupe_rows_by_columns(rows, ["id"]) == [
        {"id": "A", "v": 2},
        {"id": "B", "v": 3},
    ]


def test_dedupe_rows_by_columns_mbta_style_shuttle_short_name_collision() -> None:
    """Real-world case: MBTA's routes.txt reuses the same route_short_name
    across many distinct route_ids for shuttle-bus replacements - this is
    the exact CardinalityViolation this function exists to prevent."""
    rows = [
        {
            "route_id": "Shuttle-ManchesterRockport",
            "short_name": "Rockport Line Shuttle",
        },
        {
            "route_id": "Shuttle-RockportSalemExpress",
            "short_name": "Rockport Line Shuttle",
        },
        {"route_id": "Red", "short_name": "Red Line"},
    ]
    deduped = dedupe_rows_by_columns(rows, ["short_name"])
    assert len(deduped) == 2
    assert {row["short_name"] for row in deduped} == {
        "Rockport Line Shuttle",
        "Red Line",
    }


def test_dedupe_rows_by_columns_composite_key() -> None:
    # Trip/Stop upserts dedupe on a two-column conflict target
    # (transit_system_id, trip_id) / (stop_id, transit_system_id), not a
    # single column - same trip_id under two different systems must not
    # collide with each other.
    rows = [
        {"transit_system_id": 1, "trip_id": "T1", "name": "old"},
        {"transit_system_id": 1, "trip_id": "T1", "name": "new"},
        {"transit_system_id": 2, "trip_id": "T1", "name": "different system"},
    ]
    deduped = dedupe_rows_by_columns(rows, ["transit_system_id", "trip_id"])
    assert len(deduped) == 2
    assert {row["name"] for row in deduped} == {"new", "different system"}


def test_dedupe_rows_by_columns_empty_list() -> None:
    assert dedupe_rows_by_columns([], ["id"]) == []
