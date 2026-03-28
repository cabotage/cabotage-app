from sqlalchemy import Column, String, Boolean, Integer, DateTime
from sqlalchemy.dialects import postgresql

from cabotage.server import Model


class AuditLog(Model):
    """Read-only model backed by the audit_log SQL view."""

    __tablename__ = "audit_log"
    __table_args__ = {"info": {"is_view": True}}

    # Identity
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime)

    # Event
    verb = Column(String)
    detail = Column(String)
    object_type = Column(String)
    object_id = Column(postgresql.UUID(as_uuid=True))
    object_name = Column(String)

    # Scoping
    application_id = Column(postgresql.UUID(as_uuid=True))
    application_environment_id = Column(postgresql.UUID(as_uuid=True))

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
