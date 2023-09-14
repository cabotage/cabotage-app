"""Harmonize sqlalchemy types

Revision ID: cc8c8a00f407
Revises: d0045a6e3c29
Create Date: 2023-08-21 15:27:41.107069

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "cc8c8a00f407"
down_revision = "d0045a6e3c29"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column(
        "resources_certificate_version",
        "version_id",
        existing_type=sa.INTEGER(),
        nullable=True,
        autoincrement=False,
    )
    op.alter_column(
        "resources_ingress_version",
        "version_id",
        existing_type=sa.INTEGER(),
        nullable=True,
        autoincrement=False,
    )
    op.alter_column(
        "resources_postgres_version",
        "version_id",
        existing_type=sa.INTEGER(),
        nullable=True,
        autoincrement=False,
    )
    op.alter_column(
        "resources_redis_version",
        "version_id",
        existing_type=sa.INTEGER(),
        nullable=True,
        autoincrement=False,
    )
    op.alter_column(
        "resources_version",
        "created",
        existing_type=postgresql.TIMESTAMP(),
        nullable=True,
        autoincrement=False,
    )
    op.alter_column(
        "resources_version",
        "updated",
        existing_type=postgresql.TIMESTAMP(),
        nullable=True,
        autoincrement=False,
    )
    op.alter_column(
        "resources_version",
        "application_id",
        existing_type=postgresql.UUID(),
        nullable=True,
        autoincrement=False,
    )
    op.alter_column(
        "resources_version",
        "version_id",
        existing_type=sa.INTEGER(),
        nullable=True,
        autoincrement=False,
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column(
        "resources_version",
        "version_id",
        existing_type=sa.INTEGER(),
        nullable=False,
        autoincrement=False,
    )
    op.alter_column(
        "resources_version",
        "application_id",
        existing_type=postgresql.UUID(),
        nullable=False,
        autoincrement=False,
    )
    op.alter_column(
        "resources_version",
        "updated",
        existing_type=postgresql.TIMESTAMP(),
        nullable=False,
        autoincrement=False,
    )
    op.alter_column(
        "resources_version",
        "created",
        existing_type=postgresql.TIMESTAMP(),
        nullable=False,
        autoincrement=False,
    )
    op.alter_column(
        "resources_redis_version",
        "version_id",
        existing_type=sa.INTEGER(),
        nullable=False,
        autoincrement=False,
    )
    op.alter_column(
        "resources_postgres_version",
        "version_id",
        existing_type=sa.INTEGER(),
        nullable=False,
        autoincrement=False,
    )
    op.alter_column(
        "resources_ingress_version",
        "version_id",
        existing_type=sa.INTEGER(),
        nullable=False,
        autoincrement=False,
    )
    op.alter_column(
        "resources_certificate_version",
        "version_id",
        existing_type=sa.INTEGER(),
        nullable=False,
        autoincrement=False,
    )
    # ### end Alembic commands ###
