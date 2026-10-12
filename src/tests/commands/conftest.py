# Fixtures for tests that need a real database (currently the fetcher's
# upsert_* tests) are defined in src/tests/common.py and re-exported here so
# pytest can discover them automatically. Scoped to this directory (not
# src/tests/conftest.py) so the autouse _clean_schedule_tables fixture - and
# the Postgres container db_engine spins up - only applies to tests under
# src/tests/commands/, not the whole suite.

from ..common import (  # noqa: F401
    db_engine,
    transit_system_id,
    _clean_schedule_tables,
    make_fetcher,
)
