"""add username to app_user

Revision ID: 5c9221016515
Revises: 71927bc95ce0
Create Date: 2026-11-20 19:38:14.443655

Autogenerate detected 'uq_route_short_name' (Route's own pre-existing
drift - see 61b1854eee19/71927bc95ce0) again - stripped here too.

Hand-adjusted the column add itself: autogenerate wanted username added
directly as NOT NULL, which fails against any pre-existing app_user row
(there is at least one real one). Added nullable first, backfilled from
each row's email local-part, then set NOT NULL - this is pre-launch data
(a handful of rows, no real users yet), so a plain local-part is fine;
a collision across two pre-existing rows would fail the unique constraint
below loudly rather than silently corrupt anything.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5c9221016515'
down_revision: Union[str, Sequence[str], None] = '71927bc95ce0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('app_user', sa.Column('username', sa.String(), nullable=True))
    op.execute(
        """
        UPDATE app_user
        SET username = split_part(email, '@', 1)
        WHERE username IS NULL
        """
    )
    op.alter_column('app_user', 'username', nullable=False)
    op.create_unique_constraint('uq_app_user_username', 'app_user', ['username'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_app_user_username', 'app_user', type_='unique')
    op.drop_column('app_user', 'username')
