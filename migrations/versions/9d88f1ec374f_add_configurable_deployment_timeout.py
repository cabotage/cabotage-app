"""Add configurable deployment timeout

Revision ID: 9d88f1ec374f
Revises: 2f8f28a70d57
Create Date: 2024-06-25 00:08:48.488109

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "9d88f1ec374f"
down_revision = "2f8f28a70d57"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project_applications",
        sa.Column(
            "deployment_timeout", sa.Integer(), server_default="180", nullable=True
        ),
    )
    op.add_column(
        "project_applications_version",
        sa.Column(
            "deployment_timeout",
            sa.Integer(),
            server_default="180",
            autoincrement=False,
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("project_applications_version", "deployment_timeout")
    op.drop_column("project_applications", "deployment_timeout")
