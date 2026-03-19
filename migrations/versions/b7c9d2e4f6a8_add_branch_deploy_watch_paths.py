"""add branch_deploy_watch_paths

Revision ID: b7c9d2e4f6a8
Revises: a5088485aa53
Create Date: 2026-03-19 13:30:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "b7c9d2e4f6a8"
down_revision = "a5088485aa53"


def upgrade():
    op.add_column(
        "project_applications",
        sa.Column("branch_deploy_watch_paths", postgresql.JSONB(), nullable=True),
    )


def downgrade():
    op.drop_column("project_applications", "branch_deploy_watch_paths")
