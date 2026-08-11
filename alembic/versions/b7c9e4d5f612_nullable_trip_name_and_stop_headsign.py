"""nullable trip.name and add stop.stop_headsign

Revision ID: b7c9e4d5f612
Revises: 8666e8f948f2
Create Date: 2026-08-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c9e4d5f612'
down_revision: Union[str, Sequence[str], None] = '8666e8f948f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('trip', 'name', existing_type=sa.String(), nullable=True)
    op.add_column('stop', sa.Column('stop_headsign', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('stop', 'stop_headsign')
    op.alter_column('trip', 'name', existing_type=sa.String(), nullable=False)
