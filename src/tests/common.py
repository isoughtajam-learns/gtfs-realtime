import os
import pytest
import subprocess
import time
from pathlib import Path
from typing import Callable, Generator

from sqlalchemy import insert, delete
from sqlalchemy.engine import Engine

from src.commands.fetcher import Fetcher
from src.models import ORMBase, TransitSystem, Route, Trip, StopTime, Stop, Transfer
from src.settings import get_settings


# Real Postgres for the whole test session, from one of two sources:
#
# - CI sets TEST_DATABASE_URL (see .github/workflows/ci.yml's `test` job),
#   pointing at a Postgres already provisioned by GitHub Actions' native
#   `services:` block. That's the idiomatic way to get a real database in a
#   GH-hosted job - its image pull goes through GitHub's own infra/caching
#   and `options: --health-cmd pg_isready` gates the job on actual
#   readiness, neither of which a hand-rolled poll loop can match. That poll
#   loop is exactly what broke here: a cold pull of postgres:15-alpine on a
#   fresh runner took longer than this fixture's fixed 10-second budget
#   (20 x 0.5s), so every test using it failed with "Postgres did not start
#   in time" even though Docker itself was available and working fine.
# - Locally, where bare `pytest` has no `services:` equivalent, this
#   fixture falls back to self-managing its own throwaway Docker container,
#   same as before.
#
# Either way tests run against the same real Postgres 15 (matching prod's
# RDS engine_version) - only who provisions the container differs.
@pytest.fixture(scope="session")  # type: ignore[misc]
def db_engine(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[Engine, None, None]:
    from sqlalchemy import create_engine

    test_database_url = os.environ.get("TEST_DATABASE_URL")
    if test_database_url:
        engine = create_engine(test_database_url)
        ORMBase.metadata.create_all(engine)
        yield engine
        return

    db_name = "gtfs_test"
    container_name = f"gtfs-test-{tmp_path_factory.mktemp('pg').name}"
    port = 5433
    password = get_settings().test_db_password

    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "-e",
            f"POSTGRES_DB={db_name}",
            "-e",
            "POSTGRES_USER=postgres",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-p",
            f"{port}:5432",
            "-d",
            "postgres:15-alpine",
        ],
        check=True,
    )

    for _ in range(20):
        try:
            engine = create_engine(
                f"postgresql+psycopg2://postgres:{password}@localhost:{port}/{db_name}"
            )
            engine.connect()
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise RuntimeError("Postgres did not start in time")

    ORMBase.metadata.create_all(engine)
    yield engine

    subprocess.run(["docker", "stop", container_name], check=True)


# One TransitSystem row, shared by every test in the session rather than
# each test (or each test file's own helper) inserting its own - that was
# the actual cause of "duplicate key value violates unique constraint
# uq_name" failures: several tests called a "unique name" helper with the
# same literal name ("TS"), and since this fixture's underlying `db_engine`
# lives for the whole pytest session, every test after the first hit a real
# collision, not a flaky one. `default_schedule_url` is set to a real value
# (not None) so Route tests exercise the normal case - a route with no own
# route_url falling back to the system's default - instead of every row
# ending up with url=None (harmless either way now that missing_route_fields()
# doesn't require url at all, but this keeps the fallback path covered).
@pytest.fixture(scope="session")  # type: ignore[misc]
def transit_system_id(db_engine: Engine) -> int:
    with db_engine.begin() as conn:
        result = conn.execute(
            insert(TransitSystem)
            .values(
                name="gtfs_test_system",
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


# Runs after every test (autouse - no test needs to opt in or remember to
# call this itself). Deletes only the rows a test could have created under
# the shared transit_system_id above, scoped by that column (Route.short_name
# is unique per transit_system_id, not globally - see models.Route - so
# filtering by transit_system_id is correct here). Leaves the TransitSystem
# row itself alone so it doesn't need recreating every test.
@pytest.fixture(autouse=True)  # type: ignore[misc]
def _clean_schedule_tables(
    db_engine: Engine, transit_system_id: int
) -> Generator[None, None, None]:
    yield
    with db_engine.begin() as conn:
        for Model in (Transfer, StopTime, Stop, Route, Trip):
            conn.execute(
                delete(Model).where(Model.transit_system_id == transit_system_id)
            )


# Shared factory for every upsert_* test below: writes `content` to a single
# GTFS file (named `file_name`, e.g. fetcher.STOPS_FILE) under tmp_path, and
# returns a Fetcher already pointed at the shared transit_system_id fixture
# and reading only that one file via real_file(). Uses the `monkeypatch`
# fixture (not direct attribute assignment) for two reasons: it undoes
# itself automatically per test, and - unlike `fetcher.real_file = lambda
# ...`, which mypy's strict mode rejects as reassigning a bound method with
# an incompatible signature - monkeypatch.setattr's `value` parameter is
# untyped, so stubbing a method this way needs no type: ignore comments.
# default_schedule_url is always stubbed too (harmless for file types that
# don't call it) so routes.txt tests exercise the normal "falls back to the
# system's default" path for route_url - see Fetcher._default_schedule_url(),
# which otherwise queries the real app database, not this test's isolated one.
@pytest.fixture  # type: ignore[misc]
def make_fetcher(
    tmp_path: Path,
    transit_system_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str, str], Fetcher]:
    def _make_fetcher(file_name: str, content: str) -> Fetcher:
        file_path = tmp_path / file_name
        file_path.write_text(content)

        fetcher = Fetcher(url="", transit_system="gtfs_test_system")
        fetcher.transit_system_id = transit_system_id
        monkeypatch.setattr(
            fetcher,
            "real_file",
            lambda name: str(file_path) if name == file_name else "",
        )
        monkeypatch.setattr(
            fetcher, "_default_schedule_url", lambda: "http://example.com/routes"
        )
        return fetcher

    return _make_fetcher
