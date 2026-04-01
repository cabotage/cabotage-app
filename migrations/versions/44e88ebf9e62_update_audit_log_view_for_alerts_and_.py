"""update audit log view for alerts and integrations

Revision ID: 44e88ebf9e62
Revises: 28bcc1eb8707
Create Date: 2026-04-01 21:25:04.069936

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "44e88ebf9e62"
down_revision = "28bcc1eb8707"
branch_labels = None
depends_on = None


# fmt: off
_VIEW_SQL = """\
CREATE OR REPLACE VIEW audit_log AS

-- Configuration
SELECT a.id, t.issued_at AS timestamp,
  a.verb, a.data->>'action' AS detail, a.object_type, a.object_id, cfg.name AS object_name,
  cfg.application_id, cfg.application_environment_id,
  COALESCE(u.username, tx_u.username) AS actor_username, COALESCE(u.email, tx_u.email) AS actor_email, t.remote_addr,
  cfg.secret AS config_secret, cfg.buildtime AS config_buildtime, cfg.version_id AS config_version,
  NULL::text AS image_ref, NULL::text AS image_sha,
  NULL::integer AS deploy_release_version,
  a.data AS raw_data
FROM activity a
JOIN project_app_configurations cfg ON a.object_type = 'Configuration' AND cfg.id = a.object_id
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id

UNION ALL

-- Image (exclude complete/error status changes)
SELECT a.id, t.issued_at,
  a.verb, a.data->>'action', a.object_type, a.object_id, CONCAT('#', img.version),
  img.application_id, img.application_environment_id,
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  img.build_ref, img.image_metadata->>'sha',
  NULL,
  a.data
FROM activity a
JOIN project_app_images img ON a.object_type = 'Image' AND img.id = a.object_id
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id
WHERE a.verb NOT IN ('complete', 'error')

UNION ALL

-- Release (exclude complete/error status changes)
SELECT a.id, t.issued_at,
  a.verb, a.data->>'action', a.object_type, a.object_id, CONCAT('v', rel.version),
  rel.application_id, rel.application_environment_id,
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  NULL, NULL,
  NULL,
  a.data
FROM activity a
JOIN project_app_releases rel ON a.object_type = 'Release' AND rel.id = a.object_id
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id
WHERE a.verb NOT IN ('complete', 'error')

UNION ALL

-- Deployment (exclude complete/error)
SELECT a.id, t.issued_at,
  a.verb, a.data->>'action', a.object_type, a.object_id, NULL,
  dep.application_id, dep.application_environment_id,
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  NULL, NULL,
  dep_rel.version,
  a.data
FROM activity a
JOIN deployments dep ON a.object_type = 'Deployment' AND dep.id = a.object_id
LEFT JOIN project_app_releases dep_rel ON dep_rel.id::text = dep.release->>'id'
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id
WHERE a.verb NOT IN ('complete', 'error')

UNION ALL

-- Ingress
SELECT a.id, t.issued_at,
  a.verb, a.data->>'action', a.object_type, a.object_id, ing.name,
  ing_ae.application_id, ing.application_environment_id,
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  NULL, NULL,
  NULL,
  a.data
FROM activity a
JOIN ingresses ing ON a.object_type = 'Ingress' AND ing.id = a.object_id
LEFT JOIN application_environments ing_ae ON ing.application_environment_id = ing_ae.id
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id

UNION ALL

-- Application
SELECT a.id, t.issued_at,
  a.verb, a.data->>'action', a.object_type, a.object_id, app.name,
  app.id, NULL::uuid,
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  NULL, NULL,
  NULL,
  a.data
FROM activity a
JOIN project_applications app ON a.object_type = 'Application' AND app.id = a.object_id
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id

UNION ALL

-- Alert (firing, resolved, etc.)
SELECT a.id, t.issued_at,
  a.verb, a.data->>'action', a.object_type, a.object_id, a.data->>'alertname',
  alert.application_id, alert.application_environment_id,
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  NULL, NULL,
  NULL,
  a.data
FROM activity a
JOIN alerts alert ON a.object_type = 'Alert' AND alert.id = a.object_id
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id

UNION ALL

-- Other (ApplicationEnvironment, Organization, Environment, User, Project)
SELECT a.id, t.issued_at,
  a.verb, a.data->>'action', a.object_type, a.object_id,
  COALESCE(ae_app.name, org.name, env.name, au.username),
  ae.application_id, ae.id,
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  NULL, NULL,
  NULL,
  a.data
FROM activity a
LEFT JOIN application_environments ae ON a.object_type = 'ApplicationEnvironment' AND ae.id = a.object_id
LEFT JOIN project_applications ae_app ON ae.application_id = ae_app.id
LEFT JOIN organizations org ON a.object_type = 'Organization' AND org.id = a.object_id
LEFT JOIN project_environments env ON a.object_type = 'Environment' AND env.id = a.object_id
LEFT JOIN users au ON a.object_type = 'User' AND au.id = a.object_id
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id
WHERE a.object_type IN ('ApplicationEnvironment', 'Organization', 'Environment', 'User', 'Project')
"""
# fmt: on


def upgrade():
    from cabotage.server.models.audit import AUDIT_LOG_VIEW_SQL

    op.execute(sa.text("DROP VIEW IF EXISTS audit_log"))
    op.execute(sa.text(AUDIT_LOG_VIEW_SQL))


def downgrade():
    # Revert to previous version without Alert section
    # The previous view is maintained in migration 14392342a190
    op.execute(sa.text("DROP VIEW IF EXISTS audit_log"))
