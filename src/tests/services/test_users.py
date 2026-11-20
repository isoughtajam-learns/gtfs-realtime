from typing import Generator

import pytest
from sqlalchemy import delete
from sqlalchemy.engine import Engine

from src.models import AppUser
from src.services.users import EmailAlreadyRegistered, create_user, get_user_by_email


@pytest.fixture(autouse=True)  # type: ignore[misc]
def _clean_app_user(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    monkeypatch.setattr("src.services.users.engine", db_engine)
    yield
    with db_engine.begin() as conn:
        conn.execute(delete(AppUser))


def test_create_user_and_get_by_email() -> None:
    user_id = create_user("rider@example.com", "hashed-password")
    assert isinstance(user_id, int)

    found = get_user_by_email("rider@example.com")
    assert found is not None
    assert found["id"] == user_id
    assert found["password_hash"] == "hashed-password"


def test_create_user_rejects_duplicate_email() -> None:
    create_user("dup@example.com", "hash-1")
    with pytest.raises(EmailAlreadyRegistered):
        create_user("dup@example.com", "hash-2")


def test_get_user_by_email_unknown_returns_none() -> None:
    assert get_user_by_email("nobody@example.com") is None
