from __future__ import annotations

import datetime
import uuid
from typing import Any, TypedDict

from sqlalchemy import Boolean, DateTime, ForeignKey, String, text
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
        DateTime, default=datetime.datetime.now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=datetime.datetime.now,
        onupdate=datetime.datetime.now,
    )

    organization = relationship(
        "Organization",
        backref=backref("notification_routes", lazy="dynamic"),
    )

    def __repr__(self):
        return f"<NotificationRoute {self.id} types={self.notification_types} integration={self.integration}>"
