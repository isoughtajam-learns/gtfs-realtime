from src.services.trip_detail import _tail_stops_after_anchor


def test_tail_stops_after_anchor_returns_stops_after_last_covered() -> None:
    rows = [(1, "S1"), (2, "S2"), (3, "S3"), (4, "S4")]

    assert _tail_stops_after_anchor(rows, ["S1", "S2"]) == [(3, "S3"), (4, "S4")]


def test_tail_stops_after_anchor_empty_when_last_covered_is_final_stop() -> None:
    rows = [(1, "S1"), (2, "S2")]

    assert _tail_stops_after_anchor(rows, ["S1", "S2"]) == []


def test_tail_stops_after_anchor_empty_when_last_covered_id_not_in_schedule() -> None:
    # e.g. MBTA's realtime/schedule ID mismatch - safer to show nothing
    # extra than guess where an unrecognized stop_id fits in the sequence.
    rows = [(1, "S1"), (2, "S2")]

    assert _tail_stops_after_anchor(rows, ["not-a-real-stop-id"]) == []


def test_tail_stops_after_anchor_empty_when_rows_empty() -> None:
    # Mirrors get_scheduled_tail_stops finding zero StopTime rows for this
    # trip - either it was never in Schedule, or the transit system itself
    # is unknown.
    assert _tail_stops_after_anchor([], ["S1"]) == []


def test_tail_stops_after_anchor_empty_when_live_stop_ids_empty() -> None:
    rows = [(1, "S1"), (2, "S2")]

    assert _tail_stops_after_anchor(rows, []) == []


def test_tail_stops_after_anchor_skips_duplicate_stop_id_at_turnback_station() -> None:
    # Real BART data: SFO (Y10-1) on the Yellow line is a turnback station -
    # the train dwells and reverses, so the static schedule legitimately
    # lists the same stop_id twice in a row before continuing to Millbrae
    # (W40-3). The live feed has already reported both SFO events - the
    # backfill must jump straight to Millbrae, not re-surface SFO as a
    # bogus duplicate of a stop already shown live.
    rows = [(25, "W30-1"), (26, "Y10-1"), (27, "Y10-1"), (28, "W40-3")]

    result = _tail_stops_after_anchor(rows, ["W30-1", "Y10-1", "Y10-1"])

    assert result == [(28, "W40-3")]


def test_tail_stops_after_anchor_still_pending_second_occurrence_at_turnback_station() -> (
    None
):
    # Same turnback station, but the live feed has only reported SFO once
    # so far (the train is still dwelling, hasn't reversed yet) - the second
    # SFO event is a real, not-yet-live stop and must still be backfilled.
    rows = [(25, "W30-1"), (26, "Y10-1"), (27, "Y10-1"), (28, "W40-3")]

    result = _tail_stops_after_anchor(rows, ["W30-1", "Y10-1"])

    assert result == [(27, "Y10-1"), (28, "W40-3")]


def test_tail_stops_after_anchor_handles_distant_loop_revisit() -> None:
    # A stop_id revisited much later (not adjacently, e.g. a loop shuttle)
    # must not be confused with a turnback dwell - anchoring on the Nth
    # occurrence (not just "the last one anywhere in the schedule") keeps
    # the real intermediate loop stops in the backfill instead of skipping
    # them.
    rows = [(1, "A"), (2, "B"), (3, "C"), (4, "A"), (5, "D")]

    result = _tail_stops_after_anchor(rows, ["A"])

    assert result == [(2, "B"), (3, "C"), (4, "A"), (5, "D")]
