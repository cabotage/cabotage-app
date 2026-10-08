"""Add revocable admin passkey grants and single-use challenges.

Revision ID: b41d928e63af
Revises: 6e1f85c42b83
"""

from alembic import op
import sqlalchemy as sa

revision = "b41d928e63af"
down_revision = "6e1f85c42b83"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_grants",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "credential_id",
            sa.UUID(),
            sa.ForeignKey("webauthn.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("uniquifier", sa.String(255), nullable=False),
        sa.Column("session_binding", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    for column in ("user_id", "credential_id", "expires_at"):
        op.create_index(f"ix_admin_grants_{column}", "admin_grants", [column])
    op.create_table(
        "admin_challenges",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("uniquifier", sa.String(255), nullable=False),
        sa.Column("session_binding", sa.String(64), nullable=False),
        sa.Column(
            "grant_id",
            sa.String(64),
            sa.ForeignKey("admin_grants.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "credential_id", sa.UUID(), sa.ForeignKey("webauthn.id", ondelete="CASCADE")
        ),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("request_digest", sa.String(64)),
        sa.Column("origin", sa.String(255), nullable=False),
        sa.Column("rp_id", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("attempted_at", sa.DateTime()),
        sa.Column("verified_at", sa.DateTime()),
        sa.Column("used_at", sa.DateTime()),
    )
    for column in ("user_id", "grant_id", "expires_at"):
        op.create_index(f"ix_admin_challenges_{column}", "admin_challenges", [column])


def downgrade() -> None:
    op.drop_table("admin_challenges")
    op.drop_table("admin_grants")
