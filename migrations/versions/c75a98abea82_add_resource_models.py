"""Add Resource Models

Revision ID: c75a98abea82
Revises: 5e672db5410d
Create Date: 2023-06-18 13:51:10.035468

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c75a98abea82"
down_revision = "5e672db5410d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "resources_certificate_version",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), autoincrement=False, nullable=False
        ),
        sa.Column("version_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column(
            "transaction_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", "transaction_id", name=op.f("pk_resources_certificate_version")
        ),
    )
    op.create_index(
        op.f("ix_resources_certificate_version_end_transaction_id"),
        "resources_certificate_version",
        ["end_transaction_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_resources_certificate_version_operation_type"),
        "resources_certificate_version",
        ["operation_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_resources_certificate_version_transaction_id"),
        "resources_certificate_version",
        ["transaction_id"],
        unique=False,
    )
    op.create_table(
        "resources_ingress_version",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), autoincrement=False, nullable=False
        ),
        sa.Column("version_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column(
            "transaction_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", "transaction_id", name=op.f("pk_resources_ingress_version")
        ),
    )
    op.create_index(
        op.f("ix_resources_ingress_version_end_transaction_id"),
        "resources_ingress_version",
        ["end_transaction_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_resources_ingress_version_operation_type"),
        "resources_ingress_version",
        ["operation_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_resources_ingress_version_transaction_id"),
        "resources_ingress_version",
        ["transaction_id"],
        unique=False,
    )
    op.create_table(
        "resources_postgres_version",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), autoincrement=False, nullable=False
        ),
        sa.Column("version_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column(
            "transaction_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", "transaction_id", name=op.f("pk_resources_postgres_version")
        ),
    )
    op.create_index(
        op.f("ix_resources_postgres_version_end_transaction_id"),
        "resources_postgres_version",
        ["end_transaction_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_resources_postgres_version_operation_type"),
        "resources_postgres_version",
        ["operation_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_resources_postgres_version_transaction_id"),
        "resources_postgres_version",
        ["transaction_id"],
        unique=False,
    )
    op.create_table(
        "resources_redis_version",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), autoincrement=False, nullable=False
        ),
        sa.Column("version_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column(
            "transaction_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", "transaction_id", name=op.f("pk_resources_redis_version")
        ),
    )
    op.create_index(
        op.f("ix_resources_redis_version_end_transaction_id"),
        "resources_redis_version",
        ["end_transaction_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_resources_redis_version_operation_type"),
        "resources_redis_version",
        ["operation_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_resources_redis_version_transaction_id"),
        "resources_redis_version",
        ["transaction_id"],
        unique=False,
    )
    op.create_table(
        "resources_version",
        sa.Column("created", sa.DateTime(), autoincrement=False, nullable=False),
        sa.Column("updated", sa.DateTime(), autoincrement=False, nullable=False),
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("type", sa.String(length=50), autoincrement=False, nullable=True),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("version_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column(
            "transaction_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", "transaction_id", name=op.f("pk_resources_version")
        ),
    )
    op.create_index(
        op.f("ix_resources_version_end_transaction_id"),
        "resources_version",
        ["end_transaction_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_resources_version_operation_type"),
        "resources_version",
        ["operation_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_resources_version_transaction_id"),
        "resources_version",
        ["transaction_id"],
        unique=False,
    )
    op.create_table(
        "resources",
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("updated", sa.DateTime(), nullable=False),
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=50), nullable=True),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["project_applications.id"],
            name=op.f("fk_resources_application_id_project_applications"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources")),
    )
    op.create_table(
        "resources_certificate",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["id"], ["resources.id"], name=op.f("fk_resources_certificate_id_resources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources_certificate")),
    )
    op.create_table(
        "resources_ingress",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["id"], ["resources.id"], name=op.f("fk_resources_ingress_id_resources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources_ingress")),
    )
    op.create_table(
        "resources_postgres",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["id"], ["resources.id"], name=op.f("fk_resources_postgres_id_resources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources_postgres")),
    )
    op.create_table(
        "resources_redis",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["id"], ["resources.id"], name=op.f("fk_resources_redis_id_resources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources_redis")),
    )


def downgrade():
    op.drop_table("resources_redis")
    op.drop_table("resources_postgres")
    op.drop_table("resources_ingress")
    op.drop_table("resources_certificate")
    op.drop_table("resources")
    op.drop_index(
        op.f("ix_resources_version_transaction_id"), table_name="resources_version"
    )
    op.drop_index(
        op.f("ix_resources_version_operation_type"), table_name="resources_version"
    )
    op.drop_index(
        op.f("ix_resources_version_end_transaction_id"), table_name="resources_version"
    )
    op.drop_table("resources_version")
    op.drop_index(
        op.f("ix_resources_redis_version_transaction_id"),
        table_name="resources_redis_version",
    )
    op.drop_index(
        op.f("ix_resources_redis_version_operation_type"),
        table_name="resources_redis_version",
    )
    op.drop_index(
        op.f("ix_resources_redis_version_end_transaction_id"),
        table_name="resources_redis_version",
    )
    op.drop_table("resources_redis_version")
    op.drop_index(
        op.f("ix_resources_postgres_version_transaction_id"),
        table_name="resources_postgres_version",
    )
    op.drop_index(
        op.f("ix_resources_postgres_version_operation_type"),
        table_name="resources_postgres_version",
    )
    op.drop_index(
        op.f("ix_resources_postgres_version_end_transaction_id"),
        table_name="resources_postgres_version",
    )
    op.drop_table("resources_postgres_version")
    op.drop_index(
        op.f("ix_resources_ingress_version_transaction_id"),
        table_name="resources_ingress_version",
    )
    op.drop_index(
        op.f("ix_resources_ingress_version_operation_type"),
        table_name="resources_ingress_version",
    )
    op.drop_index(
        op.f("ix_resources_ingress_version_end_transaction_id"),
        table_name="resources_ingress_version",
    )
    op.drop_table("resources_ingress_version")
    op.drop_index(
        op.f("ix_resources_certificate_version_transaction_id"),
        table_name="resources_certificate_version",
    )
    op.drop_index(
        op.f("ix_resources_certificate_version_operation_type"),
        table_name="resources_certificate_version",
    )
    op.drop_index(
        op.f("ix_resources_certificate_version_end_transaction_id"),
        table_name="resources_certificate_version",
    )
    op.drop_table("resources_certificate_version")
