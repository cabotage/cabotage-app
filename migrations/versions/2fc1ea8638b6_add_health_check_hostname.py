"""add health check hostname

Revision ID: 2fc1ea8638b6
Revises: c75a98abea82
Create Date: 2023-07-20 16:58:09.619745

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "2fc1ea8638b6"
down_revision = "c75a98abea82"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project_app_releases",
        sa.Column("health_check_host", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "project_app_releases_version",
        sa.Column(
            "health_check_host",
            sa.String(length=256),
            autoincrement=False,
            nullable=True,
        ),
    )
    op.add_column(
        "project_applications",
        sa.Column("health_check_host", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "project_applications_version",
        sa.Column(
            "health_check_host",
            sa.String(length=256),
            autoincrement=False,
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("project_applications_version", "health_check_host")
    op.drop_column("project_applications", "health_check_host")
    op.drop_column("project_app_releases_version", "health_check_host")
    op.drop_column("project_app_releases", "health_check_host")
