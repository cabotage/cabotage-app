"""add notification_messages.reconciled_at

Revision ID: e1a7c3d9f2b4
Revises: b3bfccddb608
Create Date: 2026-10-28 18:45:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e1a7c3d9f2b4"
down_revision = "b3bfccddb608"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "notification_messages",
        sa.Column("reconciled_at", sa.DateTime(), nullable=True),
    )
    # Existing messages have already been re-sent by the reconciler (every five
    # minutes, indefinitely); mark them done so the new code doesn't re-send
    # all of them once more on deploy.
    op.execute(
        "UPDATE notification_messages SET reconciled_at = updated_at "
        "WHERE reconciled_at IS NULL"
    )


def downgrade():
    op.drop_column("notification_messages", "reconciled_at")
