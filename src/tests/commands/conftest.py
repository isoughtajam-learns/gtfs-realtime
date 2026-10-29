# Fixtures for tests that need a real database (currently the fetcher's
# upsert_* tests) are defined in src/tests/common.py and re-exported here so
# pytest can discover them automatically. Scoped to this directory (not
# src/tests/conftest.py) so the autouse _clean_schedule_tables fixture only
# applies to tests under src/tests/commands/, not the whole suite. db_engine
# itself is NOT re-exported here - it's promoted to src/tests/conftest.py
# instead (see that file's docstring for why re-exporting it per-directory
# is unsafe), and resolved for these fixtures via normal conftest
# inheritance.

from ..common import (  # noqa: F401
    transit_system_id,
    _clean_schedule_tables,
    make_fetcher,
)
