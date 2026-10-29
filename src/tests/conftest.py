# Promoted up from src/tests/commands/conftest.py so db_engine (and the one
# Postgres container it manages for the whole test session) has exactly one
# fixturedef, shared by every test subdirectory that needs it (currently
# src/tests/commands/ and src/tests/services/). Re-exporting the same
# function from a second conftest.py instead creates a SEPARATE fixturedef
# despite the identical function object - pytest then starts a second
# container on the same host port, which fails ("Bind for 0.0.0.0:5433
# failed: port is already allocated") - confirmed live running the full
# suite. Nothing else from common.py is promoted here - fixtures that depend
# on db_engine (transit_system_id, _clean_schedule_tables, etc.) stay scoped
# to whichever directory actually needs them, same as before.

from .common import db_engine  # noqa: F401
