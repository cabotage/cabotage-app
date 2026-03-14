"""backfill commit_sha into release image JSONB

Revision ID: e5f9a2c7d834
Revises: c3d8e1f2a4b5
Create Date: 2026-03-14 12:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e5f9a2c7d834"
down_revision = "c3d8e1f2a4b5"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE project_app_releases r
        SET image = r.image || jsonb_build_object(
            'commit_sha',
            COALESCE(i.image_metadata->>'sha', 'null')
        )
        FROM project_app_images i
        WHERE i.id = (r.image->>'id')::uuid
          AND r.image->>'commit_sha' IS NULL
    """
    )


def downgrade():
    op.execute(
        """
        UPDATE project_app_releases
        SET image = image - 'commit_sha'
        WHERE image ? 'commit_sha'
    """
    )
