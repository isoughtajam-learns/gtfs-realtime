"""Phase 4 (.claude/plans/development.md): the app_user table's own small
CRUD surface - see src/services/auth.py for password hashing/sessions."""

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from src.database import engine
from src.models import AppUser

# Both are indexes, not plain UniqueConstraints - see AppUser's own
# docstring for why email/username uniqueness has to be case-insensitive.
EMAIL_UNIQUE_CONSTRAINT = "uq_app_user_email_lower"
USERNAME_UNIQUE_CONSTRAINT = "uq_app_user_username_lower"


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
            # a generic one. Checks both constraint names explicitly rather
            # than defaulting unrecognized ones to the email case - these
            # are the only two unique constraints app_user has today, but
            # a future third one should surface as whatever the caller's
            # generic exception handling does with an unmapped
            # IntegrityError, not get silently misreported as "email
            # already registered".
            constraint_name = getattr(
                getattr(exc.orig, "diag", None), "constraint_name", None
            )
            if constraint_name == USERNAME_UNIQUE_CONSTRAINT:
                raise UsernameAlreadyRegistered(username) from exc
            if constraint_name == EMAIL_UNIQUE_CONSTRAINT:
                raise EmailAlreadyRegistered(email) from exc
            raise


def get_user_by_username(username: str) -> Optional[dict[str, Any]]:
    with engine.begin() as connection:
        row = connection.execute(
            select(
                AppUser.id, AppUser.email, AppUser.username, AppUser.password_hash
            ).where(func.lower(AppUser.username) == username.lower())
        ).first()
        return row._asdict() if row is not None else None
