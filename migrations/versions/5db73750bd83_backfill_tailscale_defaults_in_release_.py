"""backfill tailscale defaults in release snapshots

Revision ID: 5db73750bd83
Revises: 23d40293c277
Create Date: 2026-03-24 06:56:11.763019

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "5db73750bd83"
down_revision = "23d40293c277"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # Backfill tailscale defaults into deployment release snapshots so that
    # existing deployments don't show spurious diffs against current ingresses.
    conn.execute(
        sa.text("""
            UPDATE deployments
            SET release = jsonb_set(
                release,
                '{ingresses}',
                (
                    SELECT coalesce(jsonb_object_agg(
                        key,
                        jsonb_build_object('tailscale_hostname', null)
                            || jsonb_build_object('tailscale_funnel', false)
                            || jsonb_build_object('tailscale_tags', null)
                            || value
                    ), '{}'::jsonb)
                    FROM jsonb_each(release->'ingresses')
                )
            )
            WHERE release->'ingresses' IS NOT NULL
              AND release->'ingresses' != '{}'::jsonb
        """)
    )

    # Backfill the releases table as well
    conn.execute(
        sa.text("""
            UPDATE project_app_releases
            SET ingresses = (
                SELECT coalesce(jsonb_object_agg(
                    key,
                    jsonb_build_object('tailscale_hostname', null)
                        || jsonb_build_object('tailscale_funnel', false)
                        || jsonb_build_object('tailscale_tags', null)
                        || value
                ), '{}'::jsonb)
                FROM jsonb_each(ingresses)
            )
            WHERE ingresses IS NOT NULL
              AND ingresses != '{}'::jsonb
        """)
    )


def downgrade():
    conn = op.get_bind()

    conn.execute(
        sa.text("""
            UPDATE deployments
            SET release = jsonb_set(
                release,
                '{ingresses}',
                (
                    SELECT coalesce(jsonb_object_agg(
                        key,
                        value - 'tailscale_hostname' - 'tailscale_funnel' - 'tailscale_tags'
                    ), '{}'::jsonb)
                    FROM jsonb_each(release->'ingresses')
                )
            )
            WHERE release->'ingresses' IS NOT NULL
              AND release->'ingresses' != '{}'::jsonb
        """)
    )

    conn.execute(
        sa.text("""
            UPDATE project_app_releases
            SET ingresses = (
                SELECT coalesce(jsonb_object_agg(
                    key,
                    value - 'tailscale_hostname' - 'tailscale_funnel' - 'tailscale_tags'
                ), '{}'::jsonb)
                FROM jsonb_each(ingresses)
            )
            WHERE ingresses IS NOT NULL
              AND ingresses != '{}'::jsonb
        """)
    )
