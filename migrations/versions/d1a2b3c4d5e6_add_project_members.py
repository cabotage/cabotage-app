"""add project members table

Revision ID: d1a2b3c4d5e6
Revises: 30d4a013f3d3
Create Date: 2026-03-17 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "d1a2b3c4d5e6"
down_revision = "30d4a013f3d3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_members",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("admin", sa.Boolean(), nullable=False, server_default="false"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("user_id", "project_id"),
    )


def downgrade():
    op.drop_table("project_members")
