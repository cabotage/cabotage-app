"""Add environments support

Revision ID: c3d4e5f6a7b8
Revises: b4e7f8a9c0d1
Create Date: 2026-03-02 13:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c3d4e5f6a7b8"
down_revision = "b4e7f8a9c0d1"
branch_labels = None
depends_on = None


def upgrade():
    # Add environments_enabled to projects
    op.add_column(
        "projects",
        sa.Column(
            "environments_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "projects_version",
        sa.Column(
            "environments_enabled",
            sa.Boolean(),
            autoincrement=False,
            nullable=True,
        ),
    )

    # Create project_environments table
    op.create_table(
        "project_environments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("k8s_identifier", sa.String(64), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("ephemeral", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("ttl_hours", sa.Integer(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=True),
        sa.Column("updated", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "slug", name="uq_environments_project_slug"),
        sa.UniqueConstraint(
            "project_id", "k8s_identifier", name="uq_environments_project_k8s_id"
        ),
    )
    op.create_table(
        "project_environments_version",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column("name", sa.Text(), autoincrement=False, nullable=True),
        sa.Column("slug", sa.Text(), autoincrement=False, nullable=True),
        sa.Column("k8s_identifier", sa.String(64), autoincrement=False, nullable=True),
        sa.Column("sort_order", sa.Integer(), autoincrement=False, nullable=True),
        sa.Column("ephemeral", sa.Boolean(), autoincrement=False, nullable=True),
        sa.Column("ttl_hours", sa.Integer(), autoincrement=False, nullable=True),
        sa.Column("is_default", sa.Boolean(), autoincrement=False, nullable=True),
        sa.Column("version_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("created", sa.DateTime(), autoincrement=False, nullable=True),
        sa.Column("updated", sa.DateTime(), autoincrement=False, nullable=True),
        sa.Column(
            "transaction_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "end_transaction_id", sa.BigInteger(), nullable=True
        ),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", "transaction_id"),
    )
    op.create_index(
        "ix_project_environments_version_end_transaction_id",
        "project_environments_version",
        ["end_transaction_id"],
    )
    op.create_index(
        "ix_project_environments_version_transaction_id",
        "project_environments_version",
        ["transaction_id"],
    )
    op.create_index(
        "ix_project_environments_version_operation_type",
        "project_environments_version",
        ["operation_type"],
    )

    # Create application_environments table
    op.create_table(
        "application_environments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_applications.id"),
            nullable=False,
        ),
        sa.Column(
            "environment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_environments.id"),
            nullable=False,
        ),
        sa.Column(
            "process_counts",
            postgresql.JSONB(),
            server_default=sa.text("json_object('{}')"),
            nullable=True,
        ),
        sa.Column(
            "process_pod_classes",
            postgresql.JSONB(),
            server_default=sa.text("json_object('{}')"),
            nullable=True,
        ),
        sa.Column("deployment_timeout", sa.Integer(), nullable=True),
        sa.Column("health_check_path", sa.String(64), nullable=True),
        sa.Column("health_check_host", sa.String(256), nullable=True),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=True),
        sa.Column("updated", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "application_id",
            "environment_id",
            name="uq_app_env_application_environment",
        ),
    )
    op.create_table(
        "application_environments_version",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column(
            "environment_id",
            postgresql.UUID(as_uuid=True),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column(
            "process_counts", postgresql.JSONB(), autoincrement=False, nullable=True
        ),
        sa.Column(
            "process_pod_classes", postgresql.JSONB(), autoincrement=False, nullable=True
        ),
        sa.Column(
            "deployment_timeout", sa.Integer(), autoincrement=False, nullable=True
        ),
        sa.Column(
            "health_check_path", sa.String(64), autoincrement=False, nullable=True
        ),
        sa.Column(
            "health_check_host", sa.String(256), autoincrement=False, nullable=True
        ),
        sa.Column("version_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("created", sa.DateTime(), autoincrement=False, nullable=True),
        sa.Column("updated", sa.DateTime(), autoincrement=False, nullable=True),
        sa.Column(
            "transaction_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "end_transaction_id", sa.BigInteger(), nullable=True
        ),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", "transaction_id"),
    )
    op.create_index(
        "ix_application_environments_version_end_transaction_id",
        "application_environments_version",
        ["end_transaction_id"],
    )
    op.create_index(
        "ix_application_environments_version_transaction_id",
        "application_environments_version",
        ["transaction_id"],
    )
    op.create_index(
        "ix_application_environments_version_operation_type",
        "application_environments_version",
        ["operation_type"],
    )

    # Add nullable application_environment_id FK to existing tables
    for table, version_table in [
        ("project_app_configurations", "project_app_configurations_version"),
        ("project_app_images", "project_app_images_version"),
        ("project_app_releases", "project_app_releases_version"),
        ("deployments", "deployments_version"),
    ]:
        op.add_column(
            table,
            sa.Column(
                "application_environment_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("application_environments.id"),
                nullable=True,
            ),
        )
        op.add_column(
            version_table,
            sa.Column(
                "application_environment_id",
                postgresql.UUID(as_uuid=True),
                autoincrement=False,
                nullable=True,
            ),
        )


def downgrade():
    for table, version_table in [
        ("deployments", "deployments_version"),
        ("project_app_releases", "project_app_releases_version"),
        ("project_app_images", "project_app_images_version"),
        ("project_app_configurations", "project_app_configurations_version"),
    ]:
        op.drop_column(version_table, "application_environment_id")
        op.drop_column(table, "application_environment_id")

    op.drop_index("ix_application_environments_version_operation_type")
    op.drop_index("ix_application_environments_version_transaction_id")
    op.drop_index("ix_application_environments_version_end_transaction_id")
    op.drop_table("application_environments_version")
    op.drop_table("application_environments")
    op.drop_index("ix_project_environments_version_operation_type")
    op.drop_index("ix_project_environments_version_transaction_id")
    op.drop_index("ix_project_environments_version_end_transaction_id")
    op.drop_table("project_environments_version")
    op.drop_table("project_environments")
    op.drop_column("projects_version", "environments_enabled")
    op.drop_column("projects", "environments_enabled")
