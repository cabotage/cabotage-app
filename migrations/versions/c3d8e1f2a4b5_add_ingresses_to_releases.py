"""add ingresses columns to releases

Revision ID: c3d8e1f2a4b5
Revises: a95f942c7d60
Create Date: 2026-03-06 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c3d8e1f2a4b5"
down_revision = "a95f942c7d60"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project_app_releases",
        sa.Column(
            "ingresses",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "project_app_releases",
        sa.Column(
            "ingress_changes",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "project_app_releases_version",
        sa.Column("ingresses", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "project_app_releases_version",
        sa.Column("ingress_changes", postgresql.JSONB(), nullable=True),
    )


def downgrade():
    op.drop_column("project_app_releases_version", "ingress_changes")
    op.drop_column("project_app_releases_version", "ingresses")
    op.drop_column("project_app_releases", "ingress_changes")
    op.drop_column("project_app_releases", "ingresses")
