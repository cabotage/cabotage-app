"""add deployment_checks

Revision ID: b7d3e9a1c4f2
Revises: bd8a8bc16e88
Create Date: 2026-10-21 22:30:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "b7d3e9a1c4f2"
down_revision = "bd8a8bc16e88"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("application_environments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("deployment_checks", postgresql.JSONB(), nullable=True)
        )

    with op.batch_alter_table(
        "application_environments_version", schema=None
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "deployment_checks",
                postgresql.JSONB(),
                autoincrement=False,
                nullable=True,
            )
        )


def downgrade():
    with op.batch_alter_table(
        "application_environments_version", schema=None
    ) as batch_op:
        batch_op.drop_column("deployment_checks")

    with op.batch_alter_table("application_environments", schema=None) as batch_op:
        batch_op.drop_column("deployment_checks")
