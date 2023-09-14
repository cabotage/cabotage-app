"""add flask-security-too fs_uniquifier

Revision ID: ef78b2f946c6
Revises: 2fc1ea8638b6
Create Date: 2023-08-21 12:29:31.166553

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "ef78b2f946c6"
down_revision = "2fc1ea8638b6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column(
            "fs_uniquifier",
            sa.String(length=64),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        op.f("uq_users_fs_uniquifier"), "users", ["fs_uniquifier"]
    )
    op.add_column(
        "users_version",
        sa.Column(
            "fs_uniquifier", sa.String(length=64), autoincrement=False, nullable=True
        ),
    )


def downgrade():
    op.drop_column("users_version", "fs_uniquifier")
    op.drop_constraint(op.f("uq_users_fs_uniquifier"), "users", type_="unique")
    op.drop_column("users", "fs_uniquifier")
