"""Index platform-wide deployment ordering.

Revision ID: c62e19d85f40
Revises: b41d928e63af
"""

from alembic import op

revision = "c62e19d85f40"
down_revision = "b41d928e63af"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A backward scan serves ORDER BY created DESC, id DESC.
    op.create_index(
        "ix_deployments_created_id", "deployments", ["created", "id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_deployments_created_id", table_name="deployments")
