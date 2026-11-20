"""Phase 4 (.claude/plans/development.md): the app_user table's own small
CRUD surface - see src/services/auth.py for password hashing/sessions."""

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from src.database import engine
from src.models import AppUser


class EmailAlreadyRegistered(Exception):
    pass


def create_user(email: str, password_hash: str) -> int:
    with engine.begin() as connection:
        try:
            result = connection.execute(
                insert(AppUser)
                .values(
                    email=email,
                    password_hash=password_hash,
                    created_at=datetime.now(timezone.utc),
                )
                .returning(AppUser.id)
            )
            return result.scalar_one()
        except IntegrityError as exc:
            raise EmailAlreadyRegistered(email) from exc


def get_user_by_email(email: str) -> Optional[dict[str, Any]]:
    with engine.begin() as connection:
        row = connection.execute(
            select(AppUser.id, AppUser.email, AppUser.password_hash).where(
                AppUser.email == email
            )
        ).first()
        return row._asdict() if row is not None else None
