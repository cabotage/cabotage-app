import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cabotage.server import Model

if TYPE_CHECKING:
    from cabotage.server.models.auth import Organization, Team, User


class OrganizationMember(Model):
    __tablename__ = "organization_members"

    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        primary_key=True,
    )
    admin: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship(back_populates="organizations")
    organization: Mapped["Organization"] = relationship(back_populates="members")


class OrganizationTeam(Model):
    __tablename__ = "organization_teams"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        primary_key=True,
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("teams.id"), primary_key=True
    )

    organization: Mapped["Organization"] = relationship(back_populates="teams")
    team: Mapped["Team"] = relationship(back_populates="organizations")


class TeamMember(Model):
    __tablename__ = "team_members"

    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("teams.id"), primary_key=True
    )
    admin: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship(back_populates="teams")
    team: Mapped["Team"] = relationship(back_populates="members")
