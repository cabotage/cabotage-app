import datetime
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cabotage.server import Model

if TYPE_CHECKING:
    from cabotage.server.models.auth import Organization


class OrganizationGrafanaIntegration(Model):
    __tablename__ = "organization_grafana_integrations"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        unique=True,
        index=True,
    )
    grafana_org_id: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    membership_fingerprint: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text())
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
        onupdate=lambda: datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
    )

    organization: Mapped[Organization] = relationship(
        back_populates="grafana_integration",
    )

    @property
    def ready(self) -> bool:
        """True once a complete reconcile has produced a sync fingerprint.

        A failed or partial reconcile must never leave this true.
        """
        return self.enabled and self.membership_fingerprint is not None

    def __repr__(self) -> str:
        return f"<OrganizationGrafanaIntegration {self.id} org={self.organization_id}>"
