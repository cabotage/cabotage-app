"""Add per-environment branch and github_environment_name

Revision ID: d5e6f7a8b9c0
Revises: 63781481e33d
Create Date: 2026-03-02 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "d5e6f7a8b9c0"
down_revision = "63781481e33d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "application_environments",
        sa.Column("auto_deploy_branch", sa.Text(), nullable=True),
    )
    op.add_column(
        "application_environments",
        sa.Column("github_environment_name", sa.Text(), nullable=True),
    )
    op.add_column(
        "application_environments_version",
        sa.Column("auto_deploy_branch", sa.Text(), autoincrement=False, nullable=True),
    )
    op.add_column(
        "application_environments_version",
        sa.Column(
            "github_environment_name", sa.Text(), autoincrement=False, nullable=True
        ),
    )


def downgrade():
    op.drop_column("application_environments_version", "github_environment_name")
    op.drop_column("application_environments_version", "auto_deploy_branch")
    op.drop_column("application_environments", "github_environment_name")
    op.drop_column("application_environments", "auto_deploy_branch")
