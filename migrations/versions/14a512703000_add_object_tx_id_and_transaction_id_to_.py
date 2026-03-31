"""add object_tx_id and transaction_id to audit_log view

Revision ID: 14a512703000
Revises: 161b3c839b9f
Create Date: 2026-03-31 17:35:13.525204

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "14a512703000"
down_revision = "161b3c839b9f"
branch_labels = None
depends_on = None

# Adds a.object_tx_id and a.transaction_id to every UNION branch so that
# the application layer can look up version records for computing diffs.

# fmt: off
_VIEW_SQL = """\
CREATE OR REPLACE VIEW audit_log AS

-- Configuration
SELECT a.id, t.issued_at AS timestamp, a.object_tx_id, a.transaction_id,
  a.verb, a.data->>'action' AS detail, a.object_type, a.object_id, cfg.name AS object_name,
  cfg.application_id, cfg.application_environment_id, cfg_app.project_id, cfg_proj.organization_id,
  cfg_app.name AS app_name, cfg_proj.name AS project_name,
  COALESCE(u.username, tx_u.username) AS actor_username, COALESCE(u.email, tx_u.email) AS actor_email, t.remote_addr,
  cfg.secret AS config_secret, cfg.buildtime AS config_buildtime, cfg.version_id AS config_version,
  NULL::text AS image_ref, NULL::text AS image_sha,
  NULL::integer AS deploy_release_version,
  a.data AS raw_data
FROM activity a
JOIN project_app_configurations cfg ON a.object_type = 'Configuration' AND cfg.id = a.object_id
LEFT JOIN project_applications cfg_app ON cfg_app.id = cfg.application_id
LEFT JOIN projects cfg_proj ON cfg_proj.id = cfg_app.project_id
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id

UNION ALL

-- Image
SELECT a.id, t.issued_at, a.object_tx_id, a.transaction_id,
  a.verb, a.data->>'action', a.object_type, a.object_id, CONCAT('#', img.version),
  img.application_id, img.application_environment_id, img_app.project_id, img_proj.organization_id,
  img_app.name, img_proj.name,
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  img.build_ref, img.image_metadata->>'sha',
  NULL,
  a.data
FROM activity a
JOIN project_app_images img ON a.object_type = 'Image' AND img.id = a.object_id
LEFT JOIN project_applications img_app ON img_app.id = img.application_id
LEFT JOIN projects img_proj ON img_proj.id = img_app.project_id
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id
WHERE a.verb NOT IN ('complete', 'error')

UNION ALL

-- Release
SELECT a.id, t.issued_at, a.object_tx_id, a.transaction_id,
  a.verb, a.data->>'action', a.object_type, a.object_id, CONCAT('v', rel.version),
  rel.application_id, rel.application_environment_id, rel_app.project_id, rel_proj.organization_id,
  rel_app.name, rel_proj.name,
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  NULL, NULL,
  NULL,
  a.data
FROM activity a
JOIN project_app_releases rel ON a.object_type = 'Release' AND rel.id = a.object_id
LEFT JOIN project_applications rel_app ON rel_app.id = rel.application_id
LEFT JOIN projects rel_proj ON rel_proj.id = rel_app.project_id
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id
WHERE a.verb NOT IN ('complete', 'error')

UNION ALL

-- Deployment
SELECT a.id, t.issued_at, a.object_tx_id, a.transaction_id,
  a.verb, a.data->>'action', a.object_type, a.object_id, NULL,
  dep.application_id, dep.application_environment_id, dep_app.project_id, dep_proj.organization_id,
  dep_app.name, dep_proj.name,
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  NULL, NULL,
  dep_rel.version,
  a.data
FROM activity a
JOIN deployments dep ON a.object_type = 'Deployment' AND dep.id = a.object_id
LEFT JOIN project_applications dep_app ON dep_app.id = dep.application_id
LEFT JOIN projects dep_proj ON dep_proj.id = dep_app.project_id
LEFT JOIN project_app_releases dep_rel ON dep_rel.id::text = dep.release->>'id'
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id
WHERE a.verb NOT IN ('complete', 'error')

UNION ALL

-- Ingress
SELECT a.id, t.issued_at, a.object_tx_id, a.transaction_id,
  a.verb, a.data->>'action', a.object_type, a.object_id, ing.name,
  ing_ae.application_id, ing.application_environment_id, ing_app.project_id, ing_proj.organization_id,
  ing_app.name, ing_proj.name,
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  NULL, NULL,
  NULL,
  a.data
FROM activity a
JOIN ingresses ing ON a.object_type = 'Ingress' AND ing.id = a.object_id
LEFT JOIN application_environments ing_ae ON ing.application_environment_id = ing_ae.id
LEFT JOIN project_applications ing_app ON ing_app.id = ing_ae.application_id
LEFT JOIN projects ing_proj ON ing_proj.id = ing_app.project_id
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id

UNION ALL

-- Application
SELECT a.id, t.issued_at, a.object_tx_id, a.transaction_id,
  a.verb, a.data->>'action', a.object_type, a.object_id, app.name,
  app.id, NULL::uuid, app.project_id, proj.organization_id,
  app.name, proj.name,
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  NULL, NULL,
  NULL,
  a.data
FROM activity a
JOIN project_applications app ON a.object_type = 'Application' AND app.id = a.object_id
LEFT JOIN projects proj ON proj.id = app.project_id
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id

UNION ALL

-- Other (ApplicationEnvironment, Organization, Environment, User, Project)
SELECT a.id, t.issued_at, a.object_tx_id, a.transaction_id,
  a.verb, a.data->>'action', a.object_type, a.object_id,
  COALESCE(ae_app.name, org.name, env.name, au.username, proj_direct.name),
  ae.application_id, ae.id,
  COALESCE(ae_app.project_id, env.project_id, proj_direct.id),
  COALESCE(ae_proj.organization_id, proj_direct.organization_id, org.id),
  ae_app.name, COALESCE(ae_proj.name, proj_direct.name),
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  NULL, NULL,
  NULL,
  a.data
FROM activity a
LEFT JOIN application_environments ae ON a.object_type = 'ApplicationEnvironment' AND ae.id = a.object_id
LEFT JOIN project_applications ae_app ON ae.application_id = ae_app.id
LEFT JOIN projects ae_proj ON ae_proj.id = ae_app.project_id
LEFT JOIN organizations org ON a.object_type = 'Organization' AND org.id = a.object_id
LEFT JOIN project_environments env ON a.object_type = 'Environment' AND env.id = a.object_id
LEFT JOIN projects proj_direct ON a.object_type = 'Project' AND proj_direct.id = a.object_id
LEFT JOIN users au ON a.object_type = 'User' AND au.id = a.object_id
LEFT JOIN transaction t ON t.id = a.transaction_id
LEFT JOIN users u ON u.id::text = a.data->>'user_id'
LEFT JOIN users tx_u ON tx_u.id = t.user_id
WHERE a.object_type IN ('ApplicationEnvironment', 'Organization', 'Environment', 'User', 'Project')
"""
# fmt: on

_PREV_VIEW = "161b3c839b9f"


def upgrade():
    op.execute(sa.text("DROP VIEW IF EXISTS audit_log"))
    op.execute(sa.text(_VIEW_SQL))


def downgrade():
    # Previous migration will recreate the view without these columns
    op.execute(sa.text("DROP VIEW IF EXISTS audit_log"))
