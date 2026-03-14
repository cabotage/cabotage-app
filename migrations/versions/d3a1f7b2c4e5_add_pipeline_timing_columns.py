"""add pipeline timing columns

Revision ID: d3a1f7b2c4e5
Revises: 97ead80e7ae6
Create Date: 2026-03-14 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d3a1f7b2c4e5"
down_revision = "97ead80e7ae6"
branch_labels = None
depends_on = None

# (live_table, version_table, terminal_condition)
_TABLES = [
    ("project_app_images", "project_app_images_version", "built = true OR error = true"),
    ("project_app_releases", "project_app_releases_version", "built = true OR error = true"),
    ("deployments", "deployments_version", "complete = true OR error = true"),
]


def upgrade():
    # -- 1. Add columns to live + version tables --
    for live, version, _ in _TABLES:
        for tbl in (live, version):
            op.add_column(tbl, sa.Column("started_at", sa.DateTime(), nullable=True))
            op.add_column(tbl, sa.Column("completed_at", sa.DateTime(), nullable=True))

    # -- 2. Backfill from created/updated for terminal rows --
    for live, _, condition in _TABLES:
        op.execute(
            f"UPDATE {live} SET started_at = created, completed_at = updated "
            f"WHERE {condition}"
        )

    # -- 3. Composite indexes for aggregation queries --
    op.create_index(
        "ix_images_ae_completed",
        "project_app_images",
        ["application_environment_id", "completed_at"],
    )
    op.create_index(
        "ix_releases_ae_completed",
        "project_app_releases",
        ["application_environment_id", "completed_at"],
    )
    op.create_index(
        "ix_deployments_ae_completed",
        "deployments",
        ["application_environment_id", "completed_at"],
    )


def downgrade():
    op.drop_index("ix_deployments_ae_completed", table_name="deployments")
    op.drop_index("ix_releases_ae_completed", table_name="project_app_releases")
    op.drop_index("ix_images_ae_completed", table_name="project_app_images")

    for live, version, _ in _TABLES:
        for tbl in (version, live):
            op.drop_column(tbl, "completed_at")
            op.drop_column(tbl, "started_at")
