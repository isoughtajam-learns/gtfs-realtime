"""stop_time table

Revision ID: 5f3a8c1d9e4b
Revises: 36e8637331ba
Create Date: 2026-09-04 14:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5f3a8c1d9e4b'
down_revision: Union[str, Sequence[str], None] = '36e8637331ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'stop_time',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('transit_system_id', sa.Integer(), nullable=False),
        sa.Column('trip_id', sa.String(), nullable=False),
        sa.Column('stop_sequence', sa.Integer(), nullable=False),
        sa.Column('stop_id', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['transit_system_id'], ['transit_system.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'transit_system_id', 'trip_id', 'stop_sequence', name='uq_stop_time'
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('stop_time')
