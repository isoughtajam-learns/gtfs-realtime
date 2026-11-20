"""Phase 4 (.claude/plans/development.md): password hashing and the signed
session cookie that backs the /auth/* endpoints in main.py. Stateless - no
server-side session store or revocation list for this first slice; logout
is just clearing the cookie client+server side, which is an acceptable
limitation given the limited blast radius of a leaked cookie here."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
import jwt
from fastapi import HTTPException, Request
from starlette import status

from src.models import AuthUser
from src.settings import AppEnv, get_settings


@dataclass(frozen=True)
class SessionClaims:
    user_id: int
    email: str
    username: str


SESSION_COOKIE_NAME = "session"
SESSION_TTL = timedelta(days=30)
JWT_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_session_token(user_id: int, email: str, username: str) -> str:
    settings = get_settings()
    payload = {
        # PyJWT requires "sub" to be a string - decode_session_token casts
        # it back to int (see get_current_session).
        "sub": str(user_id),
        "email": email,
        "username": username,
        "exp": datetime.now(timezone.utc) + SESSION_TTL,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def decode_session_token(token: str) -> Optional[dict[str, Any]]:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        return None


def is_secure_cookie_env() -> bool:
    return get_settings().env == AppEnv.PROD


def get_current_session(request: Request) -> SessionClaims:
    """The one real auth dependency - decodes the session cookie once.
    get_current_user/get_current_user_id below are thin FastAPI-dependency
    wrappers over this so callers that only need one half of the claims
    (the /auth/* endpoints' AuthUser response vs. /saved_stations*'s
    numeric user_id for the FK) don't have to spell out the dict access."""

    token = request.cookies.get(SESSION_COOKIE_NAME)
    payload = decode_session_token(token) if token else None
    if (
        payload is None
        or "sub" not in payload
        or "email" not in payload
        or "username" not in payload
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    return SessionClaims(
        user_id=int(payload["sub"]),
        email=str(payload["email"]),
        username=str(payload["username"]),
    )


def get_current_user(request: Request) -> AuthUser:
    session = get_current_session(request)
    return AuthUser(email=session.email, username=session.username)


def get_current_user_id(request: Request) -> int:
    return get_current_session(request).user_id
