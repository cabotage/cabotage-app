"""Add boolean is_rollback to Deployment

Revision ID: ce4b12a41a31
Revises: 088fc773e9fe
Create Date: 2024-09-25 18:35:41.119747

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "ce4b12a41a31"
down_revision = "088fc773e9fe"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "deployments",
        sa.Column("is_rollback", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "deployments_version",
        sa.Column("is_rollback", sa.Boolean(), autoincrement=False, nullable=True),
    )


def downgrade():
    op.drop_column("deployments_version", "is_rollback")
    op.drop_column("deployments", "is_rollback")
