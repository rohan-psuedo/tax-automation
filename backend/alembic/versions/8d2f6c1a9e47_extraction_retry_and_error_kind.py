"""extraction error kind and next attempt time

Revision ID: 8d2f6c1a9e47
Revises: 269ce8692a6f
Create Date: 2026-10-03 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8d2f6c1a9e47'
down_revision: Union[str, Sequence[str], None] = '269ce8692a6f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # The rebuild must keep AUTOINCREMENT, or voucher ids could be reused (see 41534f2133cd).
    with op.batch_alter_table(
        'vouchers', schema=None, table_kwargs={'sqlite_autoincrement': True}
    ) as batch_op:
        batch_op.add_column(sa.Column('extraction_error_kind', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True))

    # Documents that could not be read only because AI reading wasn't set up (or its key was
    # rejected) are read again once the settings are fixed.
    op.execute(
        "UPDATE vouchers SET extraction_error_kind = 'settings' "
        "WHERE extraction_error LIKE 'Invoice reading is not set up%' "
        "OR extraction_error LIKE '% was rejected. An administrator can check it in Settings%'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table(
        'vouchers', schema=None, table_kwargs={'sqlite_autoincrement': True}
    ) as batch_op:
        batch_op.drop_column('next_attempt_at')
        batch_op.drop_column('extraction_error_kind')
