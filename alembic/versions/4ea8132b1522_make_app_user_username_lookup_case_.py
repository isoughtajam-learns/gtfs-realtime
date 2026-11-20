"""make app_user username lookup case-insensitive

Revision ID: 4ea8132b1522
Revises: 5c9221016515
Create Date: 2026-11-20 21:47:48.372978

Confirmed live: a plain case-sensitive UniqueConstraint on username meant
a differently-cased login attempt (e.g. "Alice" vs a username stored as
"alice") failed with the same generic 401 as a wrong password, with no
signal that case was the problem. Replaced with a functional unique index
on lower(username); get_user_by_username now matches the same way.

Autogenerate also picked up 'uq_route_short_name' (Route's own
pre-existing drift - see 61b1854eee19/71927bc95ce0/5c9221016515) again -
stripped here too.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4ea8132b1522'
down_revision: Union[str, Sequence[str], None] = '5c9221016515'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f('uq_app_user_username'), 'app_user', type_='unique')
    op.create_index('uq_app_user_username_lower', 'app_user', [sa.literal_column('lower(username)')], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_app_user_username_lower', table_name='app_user')
    op.create_unique_constraint(op.f('uq_app_user_username'), 'app_user', ['username'], postgresql_nulls_not_distinct=False)
