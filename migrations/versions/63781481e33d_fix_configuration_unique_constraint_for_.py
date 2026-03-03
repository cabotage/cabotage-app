"""fix config unique constraint for environments

Revision ID: 63781481e33d
Revises: c3d4e5f6a7b8
Create Date: 2026-03-02 22:42:22.283766

"""
from alembic import op
import sqlalchemy as sa

revision = '63781481e33d'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        'uq_project_app_configurations_application_id',
        'project_app_configurations',
        type_='unique',
    )
    op.create_unique_constraint(
        'uq_project_app_configurations_app_env_name',
        'project_app_configurations',
        ['application_id', 'application_environment_id', 'name'],
    )
    op.create_index(
        'uq_project_app_configurations_app_name_no_env',
        'project_app_configurations',
        ['application_id', 'name'],
        unique=True,
        postgresql_where=sa.text('application_environment_id IS NULL'),
    )


def downgrade():
    op.drop_index(
        'uq_project_app_configurations_app_name_no_env',
        table_name='project_app_configurations',
        postgresql_where=sa.text('application_environment_id IS NULL'),
    )
    op.drop_constraint(
        'uq_project_app_configurations_app_env_name',
        'project_app_configurations',
        type_='unique',
    )
    op.create_unique_constraint(
        'uq_project_app_configurations_application_id',
        'project_app_configurations',
        ['application_id', 'name'],
    )
