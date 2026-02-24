"""Add configurable dockerfile_path to applications

Revision ID: a1b2c3d4e5f6
Revises: c2ae2e19e1f2
Create Date: 2026-02-17 20:30:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "c2ae2e19e1f2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project_applications", sa.Column("dockerfile_path", sa.Text(), nullable=True)
    )
    op.add_column(
        "project_applications_version",
        sa.Column("dockerfile_path", sa.Text(), autoincrement=False, nullable=True),
    )


def downgrade():
    op.drop_column("project_applications_version", "dockerfile_path")
    op.drop_column("project_applications", "dockerfile_path")
