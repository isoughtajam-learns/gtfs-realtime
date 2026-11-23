from typing import Generator

import pytest
from sqlalchemy import delete
from sqlalchemy.engine import Engine

from src.models import AppUser
from src.services.users import (
    EmailAlreadyRegistered,
    UsernameAlreadyRegistered,
    create_user,
    get_user_by_username,
)


@pytest.fixture(autouse=True)  # type: ignore[misc]
def _clean_app_user(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    monkeypatch.setattr("src.services.users.engine", db_engine)
    yield
    with db_engine.begin() as conn:
        conn.execute(delete(AppUser))


def test_create_user_and_get_by_username() -> None:
    user_id = create_user("rider@example.com", "rider", "hashed-password")
    assert isinstance(user_id, int)

    found = get_user_by_username("rider")
    assert found is not None
    assert found["id"] == user_id
    assert found["email"] == "rider@example.com"
    assert found["password_hash"] == "hashed-password"


def test_create_user_rejects_duplicate_email() -> None:
    create_user("dup@example.com", "dup-one", "hash-1")
    with pytest.raises(EmailAlreadyRegistered):
        create_user("dup@example.com", "dup-two", "hash-2")


def test_create_user_rejects_duplicate_username() -> None:
    create_user("one@example.com", "dupname", "hash-1")
    with pytest.raises(UsernameAlreadyRegistered):
        create_user("two@example.com", "dupname", "hash-2")


def test_get_user_by_username_unknown_returns_none() -> None:
    assert get_user_by_username("nobody") is None


def test_username_lookup_and_uniqueness_are_case_insensitive() -> None:
    # Confirmed live: a case-sensitive match here meant a differently-cased
    # login attempt failed with the same generic 401 as a wrong password.
    user_id = create_user("rider@example.com", "RiderName", "hashed-password")

    found = get_user_by_username("ridername")
    assert found is not None
    assert found["id"] == user_id
    assert found["username"] == "RiderName"  # stored casing preserved

    assert get_user_by_username("RIDERNAME") is not None

    with pytest.raises(UsernameAlreadyRegistered):
        create_user("other@example.com", "ridername", "hash-2")
