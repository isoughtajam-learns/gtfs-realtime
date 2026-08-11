"""trip_id to string on trip and stop

Revision ID: c1d2e3f4a5b6
Revises: b7c9e4d5f612
Create Date: 2026-08-07 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = 'b7c9e4d5f612'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        'trip', 'trip_id',
        existing_type=sa.Integer(),
        type_=sa.String(),
        existing_nullable=False,
        postgresql_using='trip_id::text',
    )
    op.alter_column(
        'stop', 'trip_id',
        existing_type=sa.Integer(),
        type_=sa.String(),
        existing_nullable=False,
        postgresql_using='trip_id::text',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'stop', 'trip_id',
        existing_type=sa.String(),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using='trip_id::integer',
    )
    op.alter_column(
        'trip', 'trip_id',
        existing_type=sa.String(),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using='trip_id::integer',
    )
