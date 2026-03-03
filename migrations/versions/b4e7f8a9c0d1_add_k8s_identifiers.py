"""Add k8s_identifier columns to organizations, projects, and applications

Revision ID: b4e7f8a9c0d1
Revises: 786a1c6b2ecf
Create Date: 2026-03-02 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "b4e7f8a9c0d1"
down_revision = "786a1c6b2ecf"
branch_labels = None
depends_on = None


def upgrade():
    # Step 1: Add nullable k8s_identifier columns
    op.add_column(
        "organizations", sa.Column("k8s_identifier", sa.String(64), nullable=True)
    )
    op.add_column(
        "organizations_version",
        sa.Column("k8s_identifier", sa.String(64), autoincrement=False, nullable=True),
    )
    op.add_column(
        "projects", sa.Column("k8s_identifier", sa.String(64), nullable=True)
    )
    op.add_column(
        "projects_version",
        sa.Column("k8s_identifier", sa.String(64), autoincrement=False, nullable=True),
    )
    op.add_column(
        "project_applications",
        sa.Column("k8s_identifier", sa.String(64), nullable=True),
    )
    op.add_column(
        "project_applications_version",
        sa.Column("k8s_identifier", sa.String(64), autoincrement=False, nullable=True),
    )

    # Step 2: Backfill existing records with slug value
    # This preserves current k8s resource names — no infrastructure changes needed
    op.execute("UPDATE organizations SET k8s_identifier = slug WHERE k8s_identifier IS NULL")
    op.execute("UPDATE projects SET k8s_identifier = slug WHERE k8s_identifier IS NULL")
    op.execute(
        "UPDATE project_applications SET k8s_identifier = slug WHERE k8s_identifier IS NULL"
    )

    # Step 3: Make non-nullable
    op.alter_column("organizations", "k8s_identifier", nullable=False)
    op.alter_column("projects", "k8s_identifier", nullable=False)
    op.alter_column("project_applications", "k8s_identifier", nullable=False)

    # Step 4: Add unique constraints
    op.create_unique_constraint(
        "uq_organizations_k8s_identifier", "organizations", ["k8s_identifier"]
    )
    op.create_unique_constraint(
        "uq_projects_org_k8s_identifier",
        "projects",
        ["organization_id", "k8s_identifier"],
    )
    op.create_unique_constraint(
        "uq_applications_project_k8s_identifier",
        "project_applications",
        ["project_id", "k8s_identifier"],
    )


def downgrade():
    op.drop_constraint(
        "uq_applications_project_k8s_identifier", "project_applications", type_="unique"
    )
    op.drop_constraint("uq_projects_org_k8s_identifier", "projects", type_="unique")
    op.drop_constraint(
        "uq_organizations_k8s_identifier", "organizations", type_="unique"
    )
    op.drop_column("project_applications_version", "k8s_identifier")
    op.drop_column("project_applications", "k8s_identifier")
    op.drop_column("projects_version", "k8s_identifier")
    op.drop_column("projects", "k8s_identifier")
    op.drop_column("organizations_version", "k8s_identifier")
    op.drop_column("organizations", "k8s_identifier")
