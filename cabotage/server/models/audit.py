from sqlalchemy import BigInteger, Column, String, Boolean, Integer, DateTime
from sqlalchemy.dialects import postgresql

from cabotage.server import Model


class AuditLog(Model):
    """Read-only model backed by the audit_log SQL view."""

    __tablename__ = "audit_log"
    __table_args__ = {"info": {"is_view": True}}

    # Identity
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime)

    # Version lookup (for computing diffs from version tables)
    object_tx_id = Column(BigInteger)
    transaction_id = Column(BigInteger)

    # Event
    verb = Column(String)
    detail = Column(String)
    object_type = Column(String)
    object_id = Column(postgresql.UUID(as_uuid=True))
    object_name = Column(String)

    # Scoping
    application_id = Column(postgresql.UUID(as_uuid=True))
    application_environment_id = Column(postgresql.UUID(as_uuid=True))
    project_id = Column(postgresql.UUID(as_uuid=True))
    organization_id = Column(postgresql.UUID(as_uuid=True))

    # Context (names for display at broader scopes)
    app_name = Column(String)
    project_name = Column(String)

    # Actor
    actor_username = Column(String)
    actor_email = Column(String)
    remote_addr = Column(String)

    # Config-specific
    config_secret = Column(Boolean)
    config_buildtime = Column(Boolean)
    config_version = Column(Integer)

    # Image-specific
    image_ref = Column(String)
    image_sha = Column(String)

    # Deployment-specific
    deploy_release_version = Column(Integer)

    # Raw
    raw_data = Column(postgresql.JSONB)


# fmt: off
AUDIT_LOG_VIEW_SQL = """\
CREATE OR REPLACE VIEW audit_log AS

-- Configuration (join version table so deletes are visible)
SELECT a.id, t.issued_at AS timestamp, a.object_tx_id, a.transaction_id,
  a.verb, a.data->>'action' AS detail, a.object_type, a.object_id, cfgv.name AS object_name,
  cfgv.application_id, cfgv.application_environment_id, COALESCE(cfg_app.project_id, cfg_appv.project_id) AS project_id, COALESCE(cfg_proj.organization_id, cfg_projv.organization_id) AS organization_id,
  COALESCE(cfg_app.name, cfg_appv.name) AS app_name, COALESCE(cfg_proj.name, cfg_projv.name) AS project_name,
  COALESCE(u.username, tx_u.username) AS actor_username, COALESCE(u.email, tx_u.email) AS actor_email, t.remote_addr,
  cfgv.secret AS config_secret, cfgv.buildtime AS config_buildtime, cfgv.version_id AS config_version,
  NULL::text AS image_ref, NULL::text AS image_sha,
  NULL::integer AS deploy_release_version,
  a.data AS raw_data
FROM activity a
JOIN project_app_configurations_version cfgv ON a.object_type = 'Configuration' AND cfgv.id = a.object_id AND cfgv.transaction_id = a.object_tx_id
LEFT JOIN project_applications cfg_app ON cfg_app.id = cfgv.application_id
LEFT JOIN projects cfg_proj ON cfg_proj.id = cfg_app.project_id
LEFT JOIN project_applications_version cfg_appv ON cfg_appv.id = cfgv.application_id AND cfg_appv.transaction_id = (SELECT max(transaction_id) FROM project_applications_version WHERE id = cfgv.application_id)
LEFT JOIN projects_version cfg_projv ON cfg_projv.id = cfg_appv.project_id AND cfg_projv.transaction_id = (SELECT max(transaction_id) FROM projects_version WHERE id = cfg_appv.project_id)
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

-- Alert (firing, resolved, etc.)
SELECT a.id, t.issued_at, a.object_tx_id, a.transaction_id,
  a.verb, a.data->>'action', a.object_type, a.object_id, a.data->>'alertname',
  alert.application_id, alert.application_environment_id, alert_app.project_id, alert_proj.organization_id,
  alert_app.name, alert_proj.name,
  COALESCE(u.username, tx_u.username), COALESCE(u.email, tx_u.email), t.remote_addr,
  NULL, NULL, NULL,
  NULL, NULL,
  NULL,
  a.data
FROM activity a
JOIN alerts alert ON a.object_type = 'Alert' AND alert.id = a.object_id
LEFT JOIN project_applications alert_app ON alert_app.id = alert.application_id
LEFT JOIN projects alert_proj ON alert_proj.id = alert_app.project_id
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
