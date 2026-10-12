import pytest
import subprocess
import time
from pathlib import Path
from typing import Callable, Generator

from sqlalchemy import insert, delete
from sqlalchemy.engine import Engine

from src.commands.fetcher import Fetcher
from src.models import ORMBase, TransitSystem, Route, Trip, StopTime, Stop, Transfer


# Start a temporary Postgres DB in Docker for the whole test session.
@pytest.fixture(scope="session")  # type: ignore[misc]
def db_engine(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[Engine, None, None]:
    from sqlalchemy import create_engine

    db_name = "gtfs_test"
    container_name = f"gtfs-test-{tmp_path_factory.mktemp('pg').name}"
    port = 5433

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
            "POSTGRES_PASSWORD=postgres",
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
                f"postgresql://postgres:postgres@localhost:{port}/{db_name}"
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
# route_url falling back to the system's default - rather than every row
# being silently dropped by missing_route_fields() because both were None.
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
# the shared transit_system_id above, scoped by that column everywhere
# except Route.short_name (deliberately a *global* unique column - see
# models.Route - but every row this suite writes still has the shared
# transit_system_id, so filtering by it is still correct here). Leaves the
# TransitSystem row itself alone so it doesn't need recreating every test.
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
# system's default" path instead of every row being dropped by
# missing_route_fields() because both route_url and default were None - see
# Fetcher._default_schedule_url(), which otherwise queries the real app
# database, not this test's isolated one.
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
