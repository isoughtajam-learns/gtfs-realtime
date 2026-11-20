"""Phase 4 (.claude/plans/development.md): the app_user table's own small
CRUD surface - see src/services/auth.py for password hashing/sessions."""

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from src.database import engine
from src.models import AppUser

EMAIL_UNIQUE_CONSTRAINT = "uq_app_user_email"
USERNAME_UNIQUE_CONSTRAINT = "uq_app_user_username"


class EmailAlreadyRegistered(Exception):
    pass


class UsernameAlreadyRegistered(Exception):
    pass


def create_user(email: str, username: str, password_hash: str) -> int:
    with engine.begin() as connection:
        try:
            result = connection.execute(
                insert(AppUser)
                .values(
                    email=email,
                    username=username,
                    password_hash=password_hash,
                    created_at=datetime.now(timezone.utc),
                )
                .returning(AppUser.id)
            )
            return result.scalar_one()
        except IntegrityError as exc:
            # psycopg2 exposes which unique constraint fired via
            # orig.diag.constraint_name - distinguishing them here gives
            # the endpoint a clean "which field conflicted" 409 instead of
            # a generic one. Defaults to the email case if the constraint
            # name is ever missing/unrecognized (e.g. a driver swap) -
            # still a 409, just a less specific message.
            constraint_name = getattr(
                getattr(exc.orig, "diag", None), "constraint_name", None
            )
            if constraint_name == USERNAME_UNIQUE_CONSTRAINT:
                raise UsernameAlreadyRegistered(username) from exc
            raise EmailAlreadyRegistered(email) from exc


def get_user_by_username(username: str) -> Optional[dict[str, Any]]:
    with engine.begin() as connection:
        row = connection.execute(
            select(
                AppUser.id, AppUser.email, AppUser.username, AppUser.password_hash
            ).where(AppUser.username == username)
        ).first()
        return row._asdict() if row is not None else None
