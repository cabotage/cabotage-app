"""backfill buildtime in config snapshots

Revision ID: ad75e4f600d2
Revises: 5db73750bd83
Create Date: 2026-03-28 08:00:32.350908

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "ad75e4f600d2"
down_revision = "5db73750bd83"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # Backfill "buildtime": false into each config entry within
    # deployment release snapshots so existing deployments don't show
    # spurious config diffs against current configurations.
    conn.execute(sa.text("""
            UPDATE deployments
            SET release = jsonb_set(
                release,
                '{configuration}',
                (
                    SELECT coalesce(jsonb_object_agg(
                        key,
                        jsonb_build_object('buildtime', false) || value
                    ), '{}'::jsonb)
                    FROM jsonb_each(release->'configuration')
                )
            )
            WHERE release->'configuration' IS NOT NULL
              AND release->'configuration' != '{}'::jsonb
        """))

    # Backfill the releases table as well
    conn.execute(sa.text("""
            UPDATE project_app_releases
            SET configuration = (
                SELECT coalesce(jsonb_object_agg(
                    key,
                    jsonb_build_object('buildtime', false) || value
                ), '{}'::jsonb)
                FROM jsonb_each(configuration)
            )
            WHERE configuration IS NOT NULL
              AND configuration != '{}'::jsonb
        """))


def downgrade():
    conn = op.get_bind()

    conn.execute(sa.text("""
            UPDATE deployments
            SET release = jsonb_set(
                release,
                '{configuration}',
                (
                    SELECT coalesce(jsonb_object_agg(
                        key,
                        value - 'buildtime'
                    ), '{}'::jsonb)
                    FROM jsonb_each(release->'configuration')
                )
            )
            WHERE release->'configuration' IS NOT NULL
              AND release->'configuration' != '{}'::jsonb
        """))

    conn.execute(sa.text("""
            UPDATE project_app_releases
            SET configuration = (
                SELECT coalesce(jsonb_object_agg(
                    key,
                    value - 'buildtime'
                ), '{}'::jsonb)
                FROM jsonb_each(configuration)
            )
            WHERE configuration IS NOT NULL
              AND configuration != '{}'::jsonb
        """))
