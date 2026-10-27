"""add hooks.deployed_app_envs

Revision ID: c4e8a2f6b1d9
Revises: b7d3e9a1c4f2
Create Date: 2026-10-23 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c4e8a2f6b1d9"
down_revision = "b7d3e9a1c4f2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("hooks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("deployed_app_envs", postgresql.JSONB(), nullable=True)
        )

    with op.batch_alter_table("hooks_version", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "deployed_app_envs",
                postgresql.JSONB(),
                autoincrement=False,
                nullable=True,
            )
        )


def downgrade():
    with op.batch_alter_table("hooks_version", schema=None) as batch_op:
        batch_op.drop_column("deployed_app_envs")

    with op.batch_alter_table("hooks", schema=None) as batch_op:
        batch_op.drop_column("deployed_app_envs")
