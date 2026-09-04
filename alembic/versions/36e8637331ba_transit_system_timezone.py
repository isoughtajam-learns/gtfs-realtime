"""transit system timezone

Revision ID: 36e8637331ba
Revises: 72343655a3e2
Create Date: 2026-09-02 12:55:19.869094

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '36e8637331ba'
down_revision: Union[str, Sequence[str], None] = '72343655a3e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('transit_system', sa.Column('timezone', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('transit_system', 'timezone')
