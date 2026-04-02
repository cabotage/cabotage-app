from __future__ import annotations

import datetime
import uuid
from typing import Any, TypedDict

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship, backref

from cabotage.server import Model


class NotificationCategory(TypedDict):
    label: str
    types: dict[str, str]
    scoped: bool


NOTIFICATION_CATEGORIES: dict[str, NotificationCategory] = {
    "organization": {
        "label": "Organization",
        "types": {
            "usage": "Usage",
        },
        "scoped": False,
    },
    "usage": {
        "label": "Usage",
        "types": {
            "cpu": "CPU",
            "memory": "RAM",
            "bandwidth": "Bandwidth",
        },
        "scoped": True,
    },
    "pipeline": {
        "label": "Pipeline",
        "types": {
            "image_build": "Image Build",
            "release": "Release",
            "deploy": "Deploy",
            "image_build_failed": "Image Build Failed",
            "release_failed": "Release Failed",
            "deploy_failed": "Deploy Failed",
        },
        "scoped": True,
    },
    "health": {
        "label": "Service Health",
        "types": {
            "oom": "OOM Killed",
            "crash_restart": "Crashes & Restarts",
        },
        "scoped": True,
    },
    "http": {
        "label": "HTTP",
        "types": {
            "5xx": "5xx Errors",
            "latency": "Latency",
        },
        "scoped": True,
    },
}


class NotificationRoute(Model):
    __versioned__: dict = {}
    __tablename__ = "notification_routes"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        index=True,
    )
    notification_types: Mapped[Any] = mapped_column(
        postgresql.JSONB(), server_default=text("'[]'::jsonb")
    )
    project_ids: Mapped[Any] = mapped_column(
        postgresql.JSONB(), server_default=text("'[]'::jsonb")
    )
    environment_ids: Mapped[Any] = mapped_column(
        postgresql.JSONB(), server_default=text("'[]'::jsonb")
    )
    application_ids: Mapped[Any] = mapped_column(
        postgresql.JSONB(), server_default=text("'[]'::jsonb")
    )
    integration: Mapped[str] = mapped_column(String(32))
    channel_id: Mapped[str] = mapped_column(String(64))
    channel_name: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ),
    )

    version_id: Mapped[int] = mapped_column(Integer)

    organization = relationship(
        "Organization",
        backref=backref("notification_routes", lazy="dynamic"),
    )

    __mapper_args__ = {"version_id_col": version_id}

    def __repr__(self):
        return f"<NotificationRoute {self.id} types={self.notification_types} integration={self.integration}>"


class SentNotification(Model):
    """Tracks messages sent to external channels so they can be updated in place."""

    __tablename__ = "notification_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        index=True,
    )
    notification_type: Mapped[str] = mapped_column(String(64))
    object_type: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True))
    integration: Mapped[str] = mapped_column(String(32))
    channel_id: Mapped[str] = mapped_column(String(64))
    external_message_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ),
    )

    organization = relationship("Organization")

    __table_args__ = (
        Index(
            "ix_notification_messages_object_lookup",
            "object_type",
            "object_id",
            "integration",
            "channel_id",
        ),
    )

    def __repr__(self):
        return f"<SentNotification {self.id} {self.object_type}:{self.object_id} {self.integration}>"
