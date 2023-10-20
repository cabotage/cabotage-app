"""empty message

Revision ID: 2f8f28a70d57
Revises: cc8c8a00f407
Create Date: 2023-10-20 14:07:36.633810

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2f8f28a70d57"
down_revision = "cc8c8a00f407"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project_applications",
        sa.Column(
            "github_repository_is_private",
            sa.Boolean(),
            nullable=False,
            server_default=sa.sql.false(),
        ),
    )
    op.add_column(
        "project_applications_version",
        sa.Column(
            "github_repository_is_private",
            sa.Boolean(),
            autoincrement=False,
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("project_applications_version", "github_repository_is_private")
    op.drop_column("project_applications", "github_repository_is_private")
