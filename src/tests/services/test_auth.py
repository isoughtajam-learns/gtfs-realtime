from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.services.auth import (
    SESSION_COOKIE_NAME,
    create_session_token,
    decode_session_token,
    get_current_session,
    get_current_user,
    get_current_user_id,
    hash_password,
    verify_password,
)
from src.settings import get_settings


def _request_with_cookie(name: str, value: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"cookie", f"{name}={value}".encode())],
    }
    return Request(scope)


def _request_without_cookies() -> Request:
    return Request({"type": "http", "headers": []})


def test_hash_password_roundtrip() -> None:
    password_hash = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", password_hash)
    assert not verify_password("wrong-password", password_hash)


def test_create_and_decode_session_token() -> None:
    token = create_session_token(
        user_id=42, email="rider@example.com", username="rider"
    )
    payload = decode_session_token(token)
    assert payload is not None
    assert payload["sub"] == "42"
    assert payload["email"] == "rider@example.com"
    assert payload["username"] == "rider"


@pytest.mark.parametrize("garbage_token", ["not-a-jwt", "", "a.b.c"])  # type: ignore[misc]
def test_decode_session_token_rejects_garbage(garbage_token: str) -> None:
    assert decode_session_token(garbage_token) is None


def test_decode_session_token_rejects_expired() -> None:
    settings = get_settings()
    expired = jwt.encode(
        {
            "sub": "1",
            "email": "rider@example.com",
            "username": "rider",
            "exp": datetime.now(timezone.utc) - timedelta(days=1),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    assert decode_session_token(expired) is None


def test_get_current_session_from_real_cookie_header() -> None:
    token = create_session_token(user_id=7, email="rider@example.com", username="rider")
    claims = get_current_session(_request_with_cookie(SESSION_COOKIE_NAME, token))
    assert claims.user_id == 7
    assert claims.email == "rider@example.com"
    assert claims.username == "rider"

    user = get_current_user(_request_with_cookie(SESSION_COOKIE_NAME, token))
    assert user.email == "rider@example.com"
    assert user.username == "rider"
    assert get_current_user_id(_request_with_cookie(SESSION_COOKIE_NAME, token)) == 7


def test_get_current_session_rejects_missing_cookie() -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_current_session(_request_without_cookies())
    assert exc_info.value.status_code == 401


def test_get_current_session_rejects_garbage_cookie() -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_current_session(_request_with_cookie(SESSION_COOKIE_NAME, "garbage"))
    assert exc_info.value.status_code == 401
