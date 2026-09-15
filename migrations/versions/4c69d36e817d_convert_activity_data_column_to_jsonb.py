"""convert activity data column to jsonb

Revision ID: 4c69d36e817d
Revises: 44e88ebf9e62
Create Date: 2026-04-01 21:34:20.915698

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "4c69d36e817d"
down_revision = "44e88ebf9e62"
branch_labels = None
depends_on = None


def upgrade():
    from cabotage.server.models.audit import AUDIT_LOG_VIEW_SQL

    # The audit_log view depends on activity.data — must drop before altering
    op.execute(sa.text("DROP VIEW IF EXISTS audit_log"))
    op.execute(
        sa.text("ALTER TABLE activity ALTER COLUMN data TYPE jsonb USING data::jsonb")
    )
    op.execute(sa.text(AUDIT_LOG_VIEW_SQL))


def downgrade():
    from cabotage.server.models.audit import AUDIT_LOG_VIEW_SQL

    op.execute(sa.text("DROP VIEW IF EXISTS audit_log"))
    op.execute(
        sa.text("ALTER TABLE activity ALTER COLUMN data TYPE json USING data::json")
    )
    op.execute(sa.text(AUDIT_LOG_VIEW_SQL))
