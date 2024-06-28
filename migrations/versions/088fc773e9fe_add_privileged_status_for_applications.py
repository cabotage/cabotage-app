"""add privileged status for applications

Revision ID: 088fc773e9fe
Revises: 9d88f1ec374f
Create Date: 2024-06-28 12:54:58.913712

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "088fc773e9fe"
down_revision = "9d88f1ec374f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project_applications",
        sa.Column("privileged", sa.Boolean(), nullable=False, server_default="FALSE"),
    )
    op.add_column(
        "project_applications_version",
        sa.Column(
            "privileged",
            sa.Boolean(),
            autoincrement=False,
            nullable=True,
            server_default="FALSE",
        ),
    )


def downgrade():
    op.drop_column("project_applications_version", "privileged")
    op.drop_column("project_applications", "privileged")
