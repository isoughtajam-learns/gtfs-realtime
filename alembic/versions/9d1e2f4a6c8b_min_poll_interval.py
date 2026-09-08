"""min poll interval

Revision ID: 9d1e2f4a6c8b
Revises: 7a2c4e6f8b1d
Create Date: 2026-09-08T00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d1e2f4a6c8b'
down_revision: Union[str, Sequence[str], None] = '7a2c4e6f8b1d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'transit_system',
        sa.Column(
            'min_poll_interval_seconds',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('transit_system', 'min_poll_interval_seconds')
