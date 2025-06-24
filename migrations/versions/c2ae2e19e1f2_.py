"""

Revision ID: c2ae2e19e1f2
Revises: 088fc773e9fe
Create Date: 2025-06-24 12:04:38.139898

"""

from alembic import op
import sqlalchemy as sa

revision = "c2ae2e19e1f2"
down_revision = "088fc773e9fe"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project_applications", sa.Column("subdirectory", sa.Text(), nullable=True)
    )
    op.add_column(
        "project_applications_version",
        sa.Column("subdirectory", sa.Text(), autoincrement=False, nullable=True),
    )


def downgrade():
    op.drop_column("project_applications_version", "subdirectory")
    op.drop_column("project_applications", "subdirectory")
