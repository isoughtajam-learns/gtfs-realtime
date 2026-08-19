"""nullable stop zone_id

Revision ID: 9ad0ca9e0d7e
Revises: f5a6b7c8d9e0
Create Date: 2026-08-19 00:48:19.336718

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9ad0ca9e0d7e'
down_revision: Union[str, Sequence[str], None] = 'f5a6b7c8d9e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('stop', 'zone_id', existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('stop', 'zone_id', existing_type=sa.String(), nullable=False)
