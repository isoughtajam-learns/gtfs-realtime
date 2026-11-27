"""make app_user email lookup case-insensitive

Revision ID: 37d0850e8860
Revises: 4ea8132b1522
Create Date: 2026-11-24 22:35:49.485413

Same shape as 4ea8132b1522 (username): a plain case-sensitive
UniqueConstraint on email let two differently-cased values for "the same"
address both register. No login-by-email flow exists yet, so this wasn't
currently exploitable, but it's the same bug class as username's - made
consistent here rather than left to repeat once one exists.

Autogenerate also picked up 'uq_route_short_name' (Route's own pre-existing
drift - see 61b1854eee19/71927bc95ce0/5c9221016515/4ea8132b1522) again -
stripped here too.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '37d0850e8860'
down_revision: Union[str, Sequence[str], None] = '4ea8132b1522'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f('uq_app_user_email'), 'app_user', type_='unique')
    op.create_index('uq_app_user_email_lower', 'app_user', [sa.literal_column('lower(email)')], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_app_user_email_lower', table_name='app_user')
    op.create_unique_constraint(op.f('uq_app_user_email'), 'app_user', ['email'], postgresql_nulls_not_distinct=False)
