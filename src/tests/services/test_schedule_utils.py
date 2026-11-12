from datetime import date, datetime, timezone

from src.services.schedule_utils import (
    CalendarDateRow,
    CalendarRow,
    dedupe_rows_by_columns,
    is_earlier_stop_sequence,
    is_later_stop_sequence,
    is_outside_operating_hours,
    missing_route_fields,
    missing_stop_fields,
    parse_gtfs_time_to_seconds,
    parse_optional_float,
    parse_optional_int,
    resolve_active_service_ids,
    resolve_route_url,
    resolve_trip_headsign,
)

# Monday=0 .. Sunday=6, matching date.weekday().
WEEKDAYS_ALL = (True, True, True, True, True, True, True)
WEEKDAYS_NONE = (False, False, False, False, False, False, False)


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
    assert missing_route_fields("R1", "1") == []


def test_missing_route_fields_reports_each_missing_field() -> None:
    missing = missing_route_fields(None, None)
    assert missing == ["route_id", "route_short_name"]


def test_missing_route_fields_samtrans_style_missing_colors_still_usable() -> None:
    """Real-world case: SamTrans's routes.txt has no route_color/
    route_text_color columns at all - every route used to be dropped
    entirely (0/75 usable) because those were wrongly treated as
    required; they're cosmetic-optional per the GTFS spec."""
    assert missing_route_fields("1001", "122") == []


def test_missing_route_fields_caltrain_style_missing_long_name_still_usable() -> None:
    """Real-world case: Caltrain only sets route_long_name on 1 of its 5
    routes - route_short_name alone (e.g. "Local") is enough per the GTFS
    spec, which only requires one of the short/long name pair."""
    assert missing_route_fields("ct_local", "Local") == []


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


def test_parse_gtfs_time_to_seconds_normal() -> None:
    assert parse_gtfs_time_to_seconds("06:15:30") == 6 * 3600 + 15 * 60 + 30


def test_parse_gtfs_time_to_seconds_past_midnight_not_wrapped() -> None:
    # 25:30:00 is a real, common GTFS value for a trip past midnight - must
    # stay >= 86400, not wrap to 01:30:00, so is_outside_operating_hours can
    # tell the two apart.
    assert parse_gtfs_time_to_seconds("25:30:00") == 25 * 3600 + 30 * 60


def test_parse_gtfs_time_to_seconds_blank_or_malformed() -> None:
    assert parse_gtfs_time_to_seconds(None) is None
    assert parse_gtfs_time_to_seconds("") is None
    assert parse_gtfs_time_to_seconds("not a time") is None


def test_resolve_active_service_ids_day_of_week_match() -> None:
    weekday_service = CalendarRow(
        service_id="WEEKDAY",
        weekdays=(True, True, True, True, True, False, False),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    weekend_service = CalendarRow(
        service_id="WEEKEND",
        weekdays=(False, False, False, False, False, True, True),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    # 2026-11-16 is a Monday.
    active = resolve_active_service_ids(
        [weekday_service, weekend_service], [], date(2026, 11, 16)
    )
    assert active == {"WEEKDAY"}


def test_resolve_active_service_ids_respects_date_range() -> None:
    expired_service = CalendarRow(
        service_id="EXPIRED",
        weekdays=WEEKDAYS_ALL,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 6, 30),
    )
    active = resolve_active_service_ids([expired_service], [], date(2026, 11, 16))
    assert active == set()


def test_resolve_active_service_ids_exception_adds_service() -> None:
    # A holiday-only service with no base Calendar day at all - only
    # reachable via a calendar_dates.txt exception_type=1 row, the only way
    # service is ever defined for a calendar_dates-only feed.
    holiday_service = CalendarRow(
        service_id="HOLIDAY",
        weekdays=WEEKDAYS_NONE,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    exception = CalendarDateRow(
        service_id="HOLIDAY", date=date(2026, 11, 26), exception_type=1
    )
    active = resolve_active_service_ids(
        [holiday_service], [exception], date(2026, 11, 26)
    )
    assert active == {"HOLIDAY"}


def test_resolve_active_service_ids_exception_removes_service() -> None:
    weekday_service = CalendarRow(
        service_id="WEEKDAY",
        weekdays=WEEKDAYS_ALL,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    # Thanksgiving 2026-11-26 is a Thursday - normally a WEEKDAY service day.
    exception = CalendarDateRow(
        service_id="WEEKDAY", date=date(2026, 11, 26), exception_type=2
    )
    active = resolve_active_service_ids(
        [weekday_service], [exception], date(2026, 11, 26)
    )
    assert active == set()


def _local(y: int, m: int, d: int, hour: int, minute: int = 0) -> datetime:
    return datetime(y, m, d, hour, minute, tzinfo=timezone.utc)


def test_is_outside_operating_hours_no_calendar_data_fails_open() -> None:
    result = is_outside_operating_hours(_local(2026, 11, 16, 3), {}, [], [])
    assert result.outside_operating_hours is False


def test_is_outside_operating_hours_within_todays_window() -> None:
    service = CalendarRow(
        service_id="ALL_DAYS",
        weekdays=WEEKDAYS_ALL,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    # 05:00 - 23:00 window.
    windows = {"ALL_DAYS": (5 * 3600, 23 * 3600)}
    result = is_outside_operating_hours(
        _local(2026, 11, 16, 12), windows, [service], []
    )
    assert result.outside_operating_hours is False


def test_is_outside_operating_hours_outside_todays_window() -> None:
    service = CalendarRow(
        service_id="ALL_DAYS",
        weekdays=WEEKDAYS_ALL,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    windows = {"ALL_DAYS": (5 * 3600, 23 * 3600)}
    result = is_outside_operating_hours(_local(2026, 11, 16, 2), windows, [service], [])
    assert result.outside_operating_hours is True


def test_is_outside_operating_hours_cross_midnight_via_yesterday_service() -> None:
    # Service runs 18:00 one day through 01:30 the next (recorded as
    # 18:00:00-25:30:00, GTFS's past-midnight convention) - "now" at 01:00
    # should still be in service, attributed to *yesterday*'s service_id.
    service = CalendarRow(
        service_id="ALL_DAYS",
        weekdays=WEEKDAYS_ALL,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    windows = {"ALL_DAYS": (18 * 3600, 25 * 3600 + 30 * 60)}
    result = is_outside_operating_hours(_local(2026, 11, 16, 1), windows, [service], [])
    assert result.outside_operating_hours is False


def test_is_outside_operating_hours_next_service_starts_at_today() -> None:
    service = CalendarRow(
        service_id="ALL_DAYS",
        weekdays=WEEKDAYS_ALL,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    windows = {"ALL_DAYS": (5 * 3600, 23 * 3600)}
    result = is_outside_operating_hours(_local(2026, 11, 16, 2), windows, [service], [])
    assert result.outside_operating_hours is True
    assert result.next_service_starts_at == _local(2026, 11, 16, 5)
