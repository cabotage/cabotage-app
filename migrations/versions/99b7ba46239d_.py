"""empty message

Revision ID: 99b7ba46239d
Revises: 6d4d5e6bc617
Create Date: 2018-03-13 12:51:41.495542

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "99b7ba46239d"
down_revision = "6d4d5e6bc617"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "deployments_version",
        sa.Column("created", sa.DateTime(), autoincrement=False, nullable=True),
        sa.Column("updated", sa.DateTime(), autoincrement=False, nullable=True),
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
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
            "release",
            postgresql.JSONB(astext_type=sa.Text()),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column("version_id", sa.Integer(), autoincrement=False, nullable=True),
        sa.Column(
            "transaction_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("end_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation_type", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", "transaction_id", name=op.f("pk_deployments_version")
        ),
    )
    op.create_index(
        op.f("ix_deployments_version_end_transaction_id"),
        "deployments_version",
        ["end_transaction_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deployments_version_operation_type"),
        "deployments_version",
        ["operation_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deployments_version_transaction_id"),
        "deployments_version",
        ["transaction_id"],
        unique=False,
    )
    op.create_table(
        "deployments",
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("updated", sa.DateTime(), nullable=False),
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("release", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["project_applications.id"],
            name=op.f("fk_deployments_application_id_project_applications"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployments")),
    )
    op.add_column(
        "project_applications",
        sa.Column(
            "process_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("json_object('{}')"),
            nullable=True,
        ),
    )
    op.add_column(
        "project_applications_version",
        sa.Column(
            "process_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("json_object('{}')"),
            autoincrement=False,
            nullable=True,
        ),
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("project_applications_version", "process_counts")
    op.drop_column("project_applications", "process_counts")
    op.drop_table("deployments")
    op.drop_index(
        op.f("ix_deployments_version_transaction_id"), table_name="deployments_version"
    )
    op.drop_index(
        op.f("ix_deployments_version_operation_type"), table_name="deployments_version"
    )
    op.drop_index(
        op.f("ix_deployments_version_end_transaction_id"),
        table_name="deployments_version",
    )
    op.drop_table("deployments_version")
    # ### end Alembic commands ###
