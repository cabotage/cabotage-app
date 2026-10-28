"""add cost aws actuals

Revision ID: e7a41c9d2b35
Revises: e1a7c3d9f2b4
Create Date: 2026-10-28 16:05:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e7a41c9d2b35"
down_revision = "e1a7c3d9f2b4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cost_aws_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("bucket", sa.String(length=32), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("day", "bucket"),
    )
    with op.batch_alter_table("cost_line_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("aws_day", sa.Date(), nullable=True))
        batch_op.alter_column("run_id", existing_type=sa.UUID(), nullable=True)
        batch_op.create_index("ix_cost_line_items_aws_day", ["aws_day"], unique=False)
        batch_op.create_check_constraint(
            "ck_cost_line_items_run_or_aws_day",
            "(run_id IS NULL) <> (aws_day IS NULL)",
        )


def downgrade():
    op.execute("DELETE FROM cost_line_items WHERE run_id IS NULL")
    with op.batch_alter_table("cost_line_items", schema=None) as batch_op:
        batch_op.drop_constraint("ck_cost_line_items_run_or_aws_day", type_="check")
        batch_op.drop_index("ix_cost_line_items_aws_day")
        batch_op.alter_column("run_id", existing_type=sa.UUID(), nullable=False)
        batch_op.drop_column("aws_day")
    op.drop_table("cost_aws_daily")
