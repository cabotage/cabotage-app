from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cabotage.server import Model

FEEDBACK_KINDS = ("bug", "idea", "other")


class Feedback(Model):
    """User-submitted feedback captured from any page via the floating widget."""

    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="other")
    message: Mapped[str] = mapped_column(Text)
    page_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    page_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    view_args: Mapped[Any] = mapped_column(
        postgresql.JSONB(), server_default=text("'{}'::jsonb")
    )
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    viewport: Mapped[str | None] = mapped_column(String(32), nullable=True)
    theme: Mapped[str | None] = mapped_column(String(16), nullable=True)
    remote_addr: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ),
    )

    user = relationship("User")

    __table_args__ = (
        Index("ix_feedback_remote_addr_created_at", "remote_addr", "created_at"),
    )

    def __repr__(self):
        return f"<Feedback {self.id} kind={self.kind} endpoint={self.endpoint}>"
