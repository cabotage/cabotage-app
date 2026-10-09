"""Short-lived, revocable admin elevation and single-use WebAuthn challenges."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from cabotage.server import Model


class AdminGrant(Model):
    __tablename__ = "admin_grants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    credential_id: Mapped[UUID] = mapped_column(
        ForeignKey("webauthn.id", ondelete="CASCADE"), index=True
    )
    uniquifier: Mapped[str] = mapped_column(String(255))
    session_binding: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AdminChallenge(Model):
    __tablename__ = "admin_challenges"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    uniquifier: Mapped[str] = mapped_column(String(255))
    session_binding: Mapped[str] = mapped_column(String(64))
    grant_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_grants.id", ondelete="CASCADE"), index=True
    )
    credential_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("webauthn.id", ondelete="CASCADE")
    )
    challenge: Mapped[bytes] = mapped_column(LargeBinary)
    # Digest includes the semantic action and exact HTTP method, URL and payload.
    request_digest: Mapped[str | None] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(255))
    rp_id: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
