# Own transit_system_id/_clean_schedule_tables fixtures for this directory
# - deliberately NOT re-exported from src/tests/commands/conftest.py (that
# would recreate the duplicate-fixturedef problem src/tests/conftest.py's
# docstring describes, since commands/conftest.py's own transit_system_id
# already has one scoped there). A distinct TransitSystem.name avoids
# colliding with commands/conftest.py's "gtfs_test_system" row when both
# run against the same session-wide Postgres container (db_engine, shared
# via src/tests/conftest.py).

from typing import Generator

import pytest
from sqlalchemy import delete, insert
from sqlalchemy.engine import Engine

from src.models import Route, TransitSystem, Transfer


@pytest.fixture(scope="session")  # type: ignore[misc]
def transit_system_id(db_engine: Engine) -> int:
    with db_engine.begin() as conn:
        result = conn.execute(
            insert(TransitSystem)
            .values(
                name="gtfs_test_system_services",
                realtime_url="http://example.com/feed",
                schedule_url="http://example.com/gtfs.zip",
                default_schedule_url="http://example.com/routes",
                last_fetched_at=None,
                timezone=None,
                auth_required=False,
            )
            .returning(TransitSystem.id)
        )
        return result.scalar_one()


@pytest.fixture(autouse=True)  # type: ignore[misc]
def _clean_schedule_tables(
    db_engine: Engine, transit_system_id: int
) -> Generator[None, None, None]:
    yield
    with db_engine.begin() as conn:
        for Model in (Transfer, Route):
            conn.execute(
                delete(Model).where(Model.transit_system_id == transit_system_id)
            )
