"""add tailscale support

Revision ID: 2cef85139932
Revises: 88f230545618
Create Date: 2026-03-19 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "2cef85139932"
down_revision = "88f230545618"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tailscale_integrations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("client_secret_vault_path", sa.String(length=512), nullable=True),
        sa.Column("tailnet", sa.String(length=255), nullable=True),
        sa.Column("default_tags", sa.String(length=512), nullable=True),
        sa.Column(
            "operator_state",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("operator_version", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_tailscale_integrations_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tailscale_integrations")),
        sa.UniqueConstraint(
            "organization_id",
            name=op.f("uq_tailscale_integrations_organization_id"),
        ),
    )
    op.create_index(
        op.f("ix_tailscale_integrations_organization_id"),
        "tailscale_integrations",
        ["organization_id"],
        unique=True,
    )

    # Add tailscale fields to ingresses table
    op.add_column(
        "ingresses",
        sa.Column("tailscale_hostname", sa.String(length=253), nullable=True),
    )
    op.add_column(
        "ingresses",
        sa.Column(
            "tailscale_funnel",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "ingresses", sa.Column("tailscale_tags", sa.String(length=512), nullable=True)
    )

    # Add same columns to versioned table (SQLAlchemy-Continuum)
    op.add_column(
        "ingresses_version",
        sa.Column("tailscale_hostname", sa.String(length=253), nullable=True),
    )
    op.add_column(
        "ingresses_version",
        sa.Column("tailscale_funnel", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "ingresses_version",
        sa.Column("tailscale_tags", sa.String(length=512), nullable=True),
    )


def downgrade():
    op.drop_column("ingresses_version", "tailscale_tags")
    op.drop_column("ingresses_version", "tailscale_funnel")
    op.drop_column("ingresses_version", "tailscale_hostname")
    op.drop_column("ingresses", "tailscale_tags")
    op.drop_column("ingresses", "tailscale_funnel")
    op.drop_column("ingresses", "tailscale_hostname")
    op.drop_index(
        op.f("ix_tailscale_integrations_organization_id"),
        table_name="tailscale_integrations",
    )
    op.drop_table("tailscale_integrations")
