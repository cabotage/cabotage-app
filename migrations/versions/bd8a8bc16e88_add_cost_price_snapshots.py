"""add cost price snapshots

Revision ID: bd8a8bc16e88
Revises: 8abed32f930c
Create Date: 2026-10-26 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "bd8a8bc16e88"
down_revision = "8abed32f930c"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cost_price_snapshots",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("rates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cost_price_snapshots")),
    )
    with op.batch_alter_table("cost_price_snapshots", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_cost_price_snapshots_fetched_at"),
            ["fetched_at"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("cost_price_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cost_price_snapshots_fetched_at"))

    op.drop_table("cost_price_snapshots")
