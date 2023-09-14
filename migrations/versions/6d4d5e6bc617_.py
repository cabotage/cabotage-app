"""Add columns for buildtime configurations

Revision ID: 6d4d5e6bc617
Revises: d64c16d7ce0c
Create Date: 2018-03-10 20:39:45.816820

"""
from alembic import op
import sqlalchemy as sa


revision = "6d4d5e6bc617"
down_revision = "d64c16d7ce0c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("project_app_configurations", sa.Column("buildtime", sa.Boolean()))
    op.add_column(
        "project_app_configurations",
        sa.Column("build_key_slug", sa.Text(), nullable=True),
    )
    op.execute("UPDATE project_app_configurations SET buildtime=FALSE")
    op.alter_column("project_app_configurations", "buildtime", nullable=False)
    op.add_column(
        "project_app_configurations_version",
        sa.Column("buildtime", sa.Boolean(), autoincrement=False, nullable=True),
    )
    op.add_column(
        "project_app_configurations_version",
        sa.Column("build_key_slug", sa.Text(), autoincrement=False, nullable=True),
    )


def downgrade():
    op.drop_column("project_app_configurations_version", "buildtime")
    op.drop_column("project_app_configurations", "buildtime")
    op.drop_column("project_app_configurations_version", "build_key_slug")
    op.drop_column("project_app_configurations", "build_key_slug")
