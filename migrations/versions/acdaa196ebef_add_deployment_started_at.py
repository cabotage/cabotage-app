"""add deployment started_at

Revision ID: acdaa196ebef
Revises: c4e8a2f6b1d9
Create Date: 2026-10-08 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "acdaa196ebef"
down_revision = "c4e8a2f6b1d9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "deployments",
        sa.Column("started_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "deployments_version",
        sa.Column("started_at", sa.DateTime(), autoincrement=False, nullable=True),
    )
    op.execute("UPDATE deployments SET started_at = created")
    op.execute("UPDATE deployments_version SET started_at = created")


def downgrade():
    op.drop_column("deployments_version", "started_at")
    op.drop_column("deployments", "started_at")
