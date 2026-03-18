"""github identity use bigint for github id

Revision ID: f0735497b1aa
Revises: 3c1dd1e7577c
Create Date: 2026-03-18 22:49:31.923453

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f0735497b1aa'
down_revision = '3c1dd1e7577c'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "github_identities",
        "github_id",
        type_=sa.BigInteger(),
        existing_type=sa.Integer(),
        existing_nullable=False,
    )


def downgrade():
    op.alter_column(
        "github_identities",
        "github_id",
        type_=sa.Integer(),
        existing_type=sa.BigInteger(),
        existing_nullable=False,
    )
