"""trip detail fields

Revision ID: 72343655a3e2
Revises: 9ad0ca9e0d7e
Create Date: 2026-09-01 15:59:39.186726

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '72343655a3e2'
down_revision: Union[str, Sequence[str], None] = '9ad0ca9e0d7e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('route', sa.Column('route_type', sa.Integer(), nullable=True))
    op.add_column('trip', sa.Column('trip_short_name', sa.String(), nullable=True))
    op.add_column(
        'trip', sa.Column('wheelchair_accessible', sa.Integer(), nullable=True)
    )
    op.add_column('trip', sa.Column('bikes_allowed', sa.Integer(), nullable=True))
    op.add_column('stop', sa.Column('lat', sa.Float(), nullable=True))
    op.add_column('stop', sa.Column('lon', sa.Float(), nullable=True))
    op.add_column('stop', sa.Column('platform_code', sa.String(), nullable=True))
    op.add_column('stop', sa.Column('platform_name', sa.String(), nullable=True))
    op.add_column(
        'stop', sa.Column('wheelchair_boarding', sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('stop', 'wheelchair_boarding')
    op.drop_column('stop', 'platform_name')
    op.drop_column('stop', 'platform_code')
    op.drop_column('stop', 'lon')
    op.drop_column('stop', 'lat')
    op.drop_column('trip', 'bikes_allowed')
    op.drop_column('trip', 'wheelchair_accessible')
    op.drop_column('trip', 'trip_short_name')
    op.drop_column('route', 'route_type')
