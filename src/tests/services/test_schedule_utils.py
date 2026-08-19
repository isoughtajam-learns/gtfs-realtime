from src.services.schedule_utils import (
    is_earlier_stop_sequence,
    missing_route_fields,
    missing_stop_fields,
    parse_direction_id,
    resolve_route_url,
    resolve_trip_headsign,
)


def test_resolve_trip_headsign_prefers_direct_value() -> None:
    assert resolve_trip_headsign("Direct", "ViaStopTime", "ViaRoute") == "Direct"


def test_resolve_trip_headsign_falls_back_to_stop_time() -> None:
    assert resolve_trip_headsign(None, "ViaStopTime", "ViaRoute") == "ViaStopTime"
    assert resolve_trip_headsign("", "ViaStopTime", "ViaRoute") == "ViaStopTime"


def test_resolve_trip_headsign_falls_back_to_route_long_name() -> None:
    assert resolve_trip_headsign(None, None, "ViaRoute") == "ViaRoute"


def test_resolve_trip_headsign_none_when_all_sources_empty() -> None:
    assert resolve_trip_headsign(None, None, None) is None
    assert resolve_trip_headsign("", "", "") is None


def test_is_earlier_stop_sequence_first_candidate_always_wins() -> None:
    assert is_earlier_stop_sequence(5, None) is True


def test_is_earlier_stop_sequence_lower_number_wins() -> None:
    assert is_earlier_stop_sequence(2, 5) is True
    assert is_earlier_stop_sequence(5, 2) is False


def test_is_earlier_stop_sequence_none_candidate_never_wins() -> None:
    assert is_earlier_stop_sequence(None, 5) is False
    assert is_earlier_stop_sequence(None, None) is False


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


def test_parse_direction_id_valid_values() -> None:
    assert parse_direction_id("0") == 0
    assert parse_direction_id("1") == 1


def test_parse_direction_id_blank_or_malformed_is_none() -> None:
    assert parse_direction_id(None) is None
    assert parse_direction_id("") is None
    assert parse_direction_id("not-a-number") is None


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
    assert missing_stop_fields("T1", "Union City", "1") == []


def test_missing_stop_fields_reports_each_missing_field() -> None:
    missing = missing_stop_fields(None, "Union City", None)
    assert missing == ["trip_id (via stop_times.txt)", "zone_id"]
