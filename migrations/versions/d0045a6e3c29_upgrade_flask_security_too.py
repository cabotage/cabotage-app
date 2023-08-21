"""upgrade flask-security-too

Revision ID: d0045a6e3c29
Revises: ef78b2f946c6
Create Date: 2023-08-21 13:07:37.087598

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import flask_security

# revision identifiers, used by Alembic.
revision = 'd0045a6e3c29'
down_revision = 'ef78b2f946c6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('roles', sa.Column('permissions', flask_security.datastore.AsaList(), nullable=True))
    op.add_column('roles', sa.Column('update_datetime', sa.DateTime(), server_default=sa.text('now()'), nullable=False))
    op.add_column('roles_version', sa.Column('permissions', flask_security.datastore.AsaList(), autoincrement=False, nullable=True))
    op.add_column('roles_version', sa.Column('update_datetime', sa.DateTime(), server_default=sa.text('now()'), autoincrement=False, nullable=True))
    op.add_column('users', sa.Column('tf_primary_method', sa.String(length=64), nullable=True))
    op.add_column('users', sa.Column('tf_totp_secret', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('tf_phone_number', sa.String(length=128), nullable=True))
    op.add_column('users', sa.Column('create_datetime', sa.DateTime(), server_default=sa.text('now()'), nullable=False))
    op.add_column('users', sa.Column('update_datetime', sa.DateTime(), server_default=sa.text('now()'), nullable=False))
    op.add_column('users', sa.Column('us_totp_secrets', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('fs_webauthn_user_handle', sa.String(length=64), nullable=True))
    op.add_column('users', sa.Column('mf_recovery_codes', flask_security.datastore.AsaList(), nullable=True))
    op.add_column('users', sa.Column('us_phone_number', sa.String(length=128), nullable=True))
    op.create_unique_constraint(op.f('uq_users_fs_webauthn_user_handle'), 'users', ['fs_webauthn_user_handle'])
    op.create_unique_constraint(op.f('uq_users_us_phone_number'), 'users', ['us_phone_number'])
    op.add_column('users_version', sa.Column('tf_primary_method', sa.String(length=64), autoincrement=False, nullable=True))
    op.add_column('users_version', sa.Column('tf_totp_secret', sa.String(length=255), autoincrement=False, nullable=True))
    op.add_column('users_version', sa.Column('tf_phone_number', sa.String(length=128), autoincrement=False, nullable=True))
    op.add_column('users_version', sa.Column('create_datetime', sa.DateTime(), server_default=sa.text('now()'), autoincrement=False, nullable=True))
    op.add_column('users_version', sa.Column('update_datetime', sa.DateTime(), server_default=sa.text('now()'), autoincrement=False, nullable=True))
    op.add_column('users_version', sa.Column('us_totp_secrets', sa.Text(), autoincrement=False, nullable=True))
    op.add_column('users_version', sa.Column('fs_webauthn_user_handle', sa.String(length=64), autoincrement=False, nullable=True))
    op.add_column('users_version', sa.Column('mf_recovery_codes', flask_security.datastore.AsaList(), autoincrement=False, nullable=True))
    op.add_column('users_version', sa.Column('us_phone_number', sa.String(length=128), autoincrement=False, nullable=True))


def downgrade():
    op.drop_column('users_version', 'us_phone_number')
    op.drop_column('users_version', 'mf_recovery_codes')
    op.drop_column('users_version', 'fs_webauthn_user_handle')
    op.drop_column('users_version', 'us_totp_secrets')
    op.drop_column('users_version', 'update_datetime')
    op.drop_column('users_version', 'create_datetime')
    op.drop_column('users_version', 'tf_phone_number')
    op.drop_column('users_version', 'tf_totp_secret')
    op.drop_column('users_version', 'tf_primary_method')
    op.drop_constraint(op.f('uq_users_us_phone_number'), 'users', type_='unique')
    op.drop_constraint(op.f('uq_users_fs_webauthn_user_handle'), 'users', type_='unique')
    op.drop_column('users', 'us_phone_number')
    op.drop_column('users', 'mf_recovery_codes')
    op.drop_column('users', 'fs_webauthn_user_handle')
    op.drop_column('users', 'us_totp_secrets')
    op.drop_column('users', 'update_datetime')
    op.drop_column('users', 'create_datetime')
    op.drop_column('users', 'tf_phone_number')
    op.drop_column('users', 'tf_totp_secret')
    op.drop_column('users', 'tf_primary_method')
    op.drop_column('roles_version', 'update_datetime')
    op.drop_column('roles_version', 'permissions')
    op.drop_column('roles', 'update_datetime')
    op.drop_column('roles', 'permissions')
