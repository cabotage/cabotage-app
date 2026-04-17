"""rework resource models for backing services

Revision ID: c2d5d53ebd73
Revises: 7d2b3c334ed4
Create Date: 2026-04-12 16:22:47.704963

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "c2d5d53ebd73"
down_revision = "7d2b3c334ed4"
branch_labels = None
depends_on = None


_OLD_RESOURCE_TABLES = [
    "resources_certificate",
    "resources_ingress",
    "resources_postgres",
    "resources_redis",
    "resources",
    "resources_certificate_version",
    "resources_ingress_version",
    "resources_postgres_version",
    "resources_redis_version",
    "resources_version",
]


def _drop_tables(table_names):
    for table_name in table_names:
        op.execute(sa.text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))


def _create_backing_service_tables():
    op.create_table(
        "resources_version",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("type", sa.String(length=50), autoincrement=False, nullable=True),
        sa.Column(
            "environment_id",
            postgresql.UUID(as_uuid=True),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column("name", sa.Text(), autoincrement=False, nullable=True),
        sa.Column("slug", postgresql.CITEXT(), autoincrement=False, nullable=True),
        sa.Column(
            "k8s_identifier",
            sa.String(length=64),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column(
            "service_version",
            sa.String(length=16),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column(
            "size_class",
            sa.String(length=32),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column("storage_size", sa.Integer(), autoincrement=False, nullable=True),
        sa.Column(
            "ha_enabled",
            sa.Boolean(),
            server_default="false",
            autoincrement=False,
            nullable=True,
        ),
        sa.Column(
            "provisioning_status",
            sa.String(length=32),
            server_default="pending",
            autoincrement=False,
            nullable=True,
        ),
        sa.Column("provisioning_error", sa.Text(), autoincrement=False, nullable=True),
        sa.Column(
            "connection_info",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column("deleted_at", sa.DateTime(), autoincrement=False, nullable=True),
        sa.Column("version_id", sa.Integer(), autoincrement=False, nullable=True),
        sa.Column("created", sa.DateTime(), autoincrement=False, nullable=True),
        sa.Column("updated", sa.DateTime(), autoincrement=False, nullable=True),
        sa.Column(
            "transaction_id",
            sa.BigInteger(),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id",
            "transaction_id",
            name=op.f("pk_resources_version"),
        ),
    )
    with op.batch_alter_table("resources_version", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_resources_version_deleted_at"),
            ["deleted_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_resources_version_end_transaction_id"),
            ["end_transaction_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_resources_version_environment_id"),
            ["environment_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_resources_version_operation_type"),
            ["operation_type"],
            unique=False,
        )
        batch_op.create_index(
            "ix_resources_version_pk_transaction_id",
            ["id", sa.literal_column("transaction_id DESC")],
            unique=False,
        )
        batch_op.create_index(
            "ix_resources_version_pk_validity",
            ["id", "transaction_id", "end_transaction_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_resources_version_transaction_id"),
            ["transaction_id"],
            unique=False,
        )

    op.create_table(
        "resources",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=50), nullable=True),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", postgresql.CITEXT(), nullable=False),
        sa.Column("k8s_identifier", sa.String(length=64), nullable=False),
        sa.Column("service_version", sa.String(length=16), nullable=False),
        sa.Column("size_class", sa.String(length=32), nullable=False),
        sa.Column("storage_size", sa.Integer(), nullable=False),
        sa.Column("ha_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "provisioning_status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("provisioning_error", sa.Text(), nullable=True),
        sa.Column(
            "connection_info",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=True,
        ),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("updated", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["project_environments.id"],
            name=op.f("fk_resources_environment_id_project_environments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources")),
        sa.UniqueConstraint(
            "environment_id",
            "k8s_identifier",
            name="uq_resources_env_k8s_identifier",
        ),
        sa.UniqueConstraint(
            "environment_id",
            "slug",
            name="uq_resources_environment_id_slug",
        ),
    )
    with op.batch_alter_table("resources", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_resources_deleted_at"),
            ["deleted_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_resources_environment_id"),
            ["environment_id"],
            unique=False,
        )

    op.create_table(
        "resources_postgres_version",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), autoincrement=False, nullable=False
        ),
        sa.Column(
            "backup_strategy",
            sa.String(length=16),
            server_default="daily",
            autoincrement=False,
            nullable=True,
        ),
        sa.Column(
            "postgres_parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column(
            "transaction_id",
            sa.BigInteger(),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id",
            "transaction_id",
            name=op.f("pk_resources_postgres_version"),
        ),
    )
    with op.batch_alter_table("resources_postgres_version", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_resources_postgres_version_end_transaction_id"),
            ["end_transaction_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_resources_postgres_version_operation_type"),
            ["operation_type"],
            unique=False,
        )
        batch_op.create_index(
            "ix_resources_postgres_version_pk_transaction_id",
            ["id", sa.literal_column("transaction_id DESC")],
            unique=False,
        )
        batch_op.create_index(
            "ix_resources_postgres_version_pk_validity",
            ["id", "transaction_id", "end_transaction_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_resources_postgres_version_transaction_id"),
            ["transaction_id"],
            unique=False,
        )

    op.create_table(
        "resources_postgres",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "backup_strategy",
            sa.String(length=16),
            server_default="daily",
            nullable=False,
        ),
        sa.Column(
            "postgres_parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["id"],
            ["resources.id"],
            name=op.f("fk_resources_postgres_id_resources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources_postgres")),
    )

    op.create_table(
        "resources_redis_version",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), autoincrement=False, nullable=False
        ),
        sa.Column(
            "leader_replicas",
            sa.Integer(),
            server_default="3",
            autoincrement=False,
            nullable=True,
        ),
        sa.Column(
            "follower_replicas",
            sa.Integer(),
            server_default="3",
            autoincrement=False,
            nullable=True,
        ),
        sa.Column(
            "transaction_id",
            sa.BigInteger(),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id",
            "transaction_id",
            name=op.f("pk_resources_redis_version"),
        ),
    )
    with op.batch_alter_table("resources_redis_version", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_resources_redis_version_end_transaction_id"),
            ["end_transaction_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_resources_redis_version_operation_type"),
            ["operation_type"],
            unique=False,
        )
        batch_op.create_index(
            "ix_resources_redis_version_pk_transaction_id",
            ["id", sa.literal_column("transaction_id DESC")],
            unique=False,
        )
        batch_op.create_index(
            "ix_resources_redis_version_pk_validity",
            ["id", "transaction_id", "end_transaction_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_resources_redis_version_transaction_id"),
            ["transaction_id"],
            unique=False,
        )

    op.create_table(
        "resources_redis",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("leader_replicas", sa.Integer(), server_default="3", nullable=False),
        sa.Column(
            "follower_replicas",
            sa.Integer(),
            server_default="3",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["id"],
            ["resources.id"],
            name=op.f("fk_resources_redis_id_resources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources_redis")),
    )

    with op.batch_alter_table(
        "project_environment_configurations", schema=None
    ) as batch_op:
        batch_op.add_column(sa.Column("resource_id", sa.UUID(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_project_environment_configurations_resource_id"),
            ["resource_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_project_environment_configurations_resource_id_resources"),
            "resources",
            ["resource_id"],
            ["id"],
        )

    with op.batch_alter_table(
        "project_environment_configurations_version", schema=None
    ) as batch_op:
        batch_op.add_column(
            sa.Column("resource_id", sa.UUID(), autoincrement=False, nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_project_environment_configurations_version_resource_id"),
            ["resource_id"],
            unique=False,
        )


def _create_legacy_resource_tables():
    op.create_table(
        "resources_certificate_version",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), autoincrement=False, nullable=False
        ),
        sa.Column("version_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column(
            "transaction_id",
            sa.BigInteger(),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id",
            "transaction_id",
            name=op.f("pk_resources_certificate_version"),
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
            "transaction_id",
            sa.BigInteger(),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id",
            "transaction_id",
            name=op.f("pk_resources_ingress_version"),
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
            "transaction_id",
            sa.BigInteger(),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id",
            "transaction_id",
            name=op.f("pk_resources_postgres_version"),
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
            "transaction_id",
            sa.BigInteger(),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id",
            "transaction_id",
            name=op.f("pk_resources_redis_version"),
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
            "transaction_id",
            sa.BigInteger(),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id",
            "transaction_id",
            name=op.f("pk_resources_version"),
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
            ["id"],
            ["resources.id"],
            name=op.f("fk_resources_certificate_id_resources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources_certificate")),
    )

    op.create_table(
        "resources_ingress",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["id"],
            ["resources.id"],
            name=op.f("fk_resources_ingress_id_resources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources_ingress")),
    )

    op.create_table(
        "resources_postgres",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["id"],
            ["resources.id"],
            name=op.f("fk_resources_postgres_id_resources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources_postgres")),
    )

    op.create_table(
        "resources_redis",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["id"],
            ["resources.id"],
            name=op.f("fk_resources_redis_id_resources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources_redis")),
    )


def upgrade():
    _drop_tables(_OLD_RESOURCE_TABLES)
    _create_backing_service_tables()


def downgrade():
    with op.batch_alter_table(
        "project_environment_configurations_version", schema=None
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f("ix_project_environment_configurations_version_resource_id")
        )
        batch_op.drop_column("resource_id")

    with op.batch_alter_table(
        "project_environment_configurations", schema=None
    ) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_project_environment_configurations_resource_id_resources"),
            type_="foreignkey",
        )
        batch_op.drop_index(
            batch_op.f("ix_project_environment_configurations_resource_id")
        )
        batch_op.drop_column("resource_id")

    _drop_tables(
        [
            "resources_postgres",
            "resources_redis",
            "resources",
            "resources_postgres_version",
            "resources_redis_version",
            "resources_version",
        ]
    )
    _create_legacy_resource_tables()
