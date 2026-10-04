"""store configuration values as text

Revision ID: 8f2c4a6d9b01
Revises: 6e1f85c42b83
Create Date: 2026-10-04

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "8f2c4a6d9b01"
down_revision = "6e1f85c42b83"
branch_labels = None
depends_on = None

_TABLES = (
    "project_app_configurations",
    "project_app_configurations_version",
    "project_environment_configurations",
    "project_environment_configurations_version",
)


def upgrade() -> None:
    # audit_log references configuration metadata, not value. Its column-level
    # dependencies do not require dropping the view for this change.
    for table in _TABLES:
        op.alter_column(table, "value", existing_type=sa.String(2048), type_=sa.Text())


def downgrade() -> None:
    # Keep writes out between the length check and narrowing all four columns.
    op.execute(sa.text(f"LOCK TABLE {', '.join(_TABLES)} IN ACCESS EXCLUSIVE MODE"))
    for table in _TABLES:
        # PostgreSQL casts can truncate long values (including trailing spaces).
        # Refuse the downgrade instead, including when only history is too long.
        op.execute(
            sa.text(
                f"""DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM {table} WHERE char_length(value) > 2048) THEN
                        RAISE EXCEPTION 'Cannot downgrade: {table}.value contains values longer than 2048 characters';
                    END IF;
                END $$"""
            )
        )
    for table in _TABLES:
        op.alter_column(table, "value", existing_type=sa.Text(), type_=sa.String(2048))
