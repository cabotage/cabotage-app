"""Add configurable health check paths

Revision ID: 5e672db5410d
Revises: ae255b391562
Create Date: 2023-06-14 17:53:11.429522

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "5e672db5410d"
down_revision = "ae255b391562"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project_app_releases",
        sa.Column(
            "health_check_path",
            sa.String(length=64),
            server_default="/_health/",
            nullable=False,
        ),
    )
    op.add_column(
        "project_app_releases_version",
        sa.Column(
            "health_check_path",
            sa.String(length=64),
            server_default="/_health/",
            autoincrement=False,
            nullable=True,
        ),
    )
    op.add_column(
        "project_applications",
        sa.Column(
            "health_check_path",
            sa.String(length=64),
            server_default="/_health/",
            nullable=False,
        ),
    )
    op.add_column(
        "project_applications_version",
        sa.Column(
            "health_check_path",
            sa.String(length=64),
            server_default="/_health/",
            autoincrement=False,
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("project_applications_version", "health_check_path")
    op.drop_column("project_applications", "health_check_path")
    op.drop_column("project_app_releases_version", "health_check_path")
    op.drop_column("project_app_releases", "health_check_path")
