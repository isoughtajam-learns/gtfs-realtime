# src/tests/commands/test_fetcher_calendar.py
from datetime import date
from typing import Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.commands import fetcher as fetcher_module
from src.commands.fetcher import Fetcher
from src.models import Calendar, CalendarDate


def test_upsert_calendar_inserts_valid_and_skips_invalid(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(
        fetcher_module.CALENDAR_FILE,
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "WEEKDAY,1,1,1,1,1,0,0,20260101,20261231\n"
        "WEEKEND,0,0,0,0,0,1,1,20260101,20261231\n"
        ",1,1,1,1,1,0,0,20260101,20261231\n",  # missing service_id - skipped
    )

    with db_engine.begin() as conn:
        fetcher.upsert_calendar(conn)
        rows = conn.execute(select(Calendar)).fetchall()

    assert len(rows) == 2
    weekday = next(r for r in rows if r.service_id == "WEEKDAY")
    assert weekday.monday is True
    assert weekday.saturday is False
    assert weekday.start_date == date(2026, 1, 1)
    assert weekday.end_date == date(2026, 12, 31)

    weekend = next(r for r in rows if r.service_id == "WEEKEND")
    assert weekend.saturday is True
    assert weekend.monday is False


def test_upsert_calendar_update_on_conflict(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(
        fetcher_module.CALENDAR_FILE,
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "WEEKDAY,1,1,1,1,1,0,0,20260101,20261231\n",
    )
    with db_engine.begin() as conn:
        fetcher.upsert_calendar(conn)

    fetcher2 = make_fetcher(
        fetcher_module.CALENDAR_FILE,
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "WEEKDAY,0,0,0,0,0,1,1,20260201,20261130\n",
    )
    with db_engine.begin() as conn:
        fetcher2.upsert_calendar(conn)
        rows = conn.execute(select(Calendar)).fetchall()

    assert len(rows) == 1
    assert rows[0].monday is False
    assert rows[0].saturday is True
    assert rows[0].start_date == date(2026, 2, 1)
    assert rows[0].end_date == date(2026, 11, 30)


def test_upsert_calendar_missing_file_is_noop(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    # calendar.txt is optional - a feed defining service entirely via
    # calendar_dates.txt has no calendar.txt at all.
    fetcher = make_fetcher(fetcher_module.TRANSFERS_FILE, "")
    with db_engine.begin() as conn:
        fetcher.upsert_calendar(conn)  # should not raise
        rows = conn.execute(select(Calendar)).fetchall()
    assert rows == []


def test_upsert_calendar_dates_inserts_valid_and_skips_invalid(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(
        fetcher_module.CALENDAR_DATES_FILE,
        "service_id,date,exception_type\n"
        "WEEKDAY,20261126,2\n"
        "HOLIDAY,20261126,1\n"
        ",20261126,1\n",  # missing service_id - skipped
    )

    with db_engine.begin() as conn:
        fetcher.upsert_calendar_dates(conn)
        rows = conn.execute(select(CalendarDate)).fetchall()

    assert len(rows) == 2
    removed = next(r for r in rows if r.service_id == "WEEKDAY")
    assert removed.date == date(2026, 11, 26)
    assert removed.exception_type == 2

    added = next(r for r in rows if r.service_id == "HOLIDAY")
    assert added.exception_type == 1


def test_upsert_calendar_dates_update_on_conflict(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(
        fetcher_module.CALENDAR_DATES_FILE,
        "service_id,date,exception_type\nWEEKDAY,20261126,2\n",
    )
    with db_engine.begin() as conn:
        fetcher.upsert_calendar_dates(conn)

    fetcher2 = make_fetcher(
        fetcher_module.CALENDAR_DATES_FILE,
        "service_id,date,exception_type\nWEEKDAY,20261126,1\n",
    )
    with db_engine.begin() as conn:
        fetcher2.upsert_calendar_dates(conn)
        rows = conn.execute(select(CalendarDate)).fetchall()

    assert len(rows) == 1
    assert rows[0].exception_type == 1


def test_upsert_calendar_dates_missing_file_is_noop(
    db_engine: Engine, make_fetcher: Callable[[str, str], Fetcher]
) -> None:
    fetcher = make_fetcher(fetcher_module.TRANSFERS_FILE, "")
    with db_engine.begin() as conn:
        fetcher.upsert_calendar_dates(conn)  # should not raise
        rows = conn.execute(select(CalendarDate)).fetchall()
    assert rows == []
