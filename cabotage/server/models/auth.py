from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING

from flask_security.models.fsqla_v3 import (
    FsModels,
    FsRoleMixin,
    FsUserMixin,
    FsWebAuthnMixin,
)
from sqlalchemy import (
    Boolean,
    DateTime,
    BigInteger,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship, backref
from sqlalchemy_continuum import make_versioned

from cabotage.server import db, Model
from cabotage.server.models.plugins import ActivityPlugin
from cabotage.server.models.utils import generate_k8s_identifier, slugify

from .auth_associations import (
    OrganizationMember,
    OrganizationTeam,
    TeamMember,
)

if TYPE_CHECKING:
    from cabotage.server.models.projects import Project

# Must be set before model classes are defined — FsUserMixin uses
# FsModels.db to create the webauthn relationship.
FsModels.db = db  # type: ignore[assignment]

activity_plugin = ActivityPlugin()
make_versioned(plugins=[activity_plugin])


roles_users = db.Table(
    "roles_users",
    db.Column("user_id", postgresql.UUID(as_uuid=True), db.ForeignKey("users.id")),
    db.Column("role_id", postgresql.UUID(as_uuid=True), db.ForeignKey("roles.id")),
)


class Role(Model, FsRoleMixin):
    __versioned__: dict = {}
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    name: Mapped[str | None] = mapped_column(String(80), unique=True)
    description: Mapped[str | None] = mapped_column(String(255))

    def __str__(self):
        return self.name

    def __hash__(self):
        return hash(self.name)


class User(Model, FsUserMixin):
    __versioned__: dict = {
        "exclude": ["password"],
    }
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    username: Mapped[str] = mapped_column(String(255), unique=True)
    password: Mapped[str] = mapped_column(String(255))

    admin: Mapped[bool] = mapped_column(Boolean, default=False)
    registered_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.now
    )

    roles: Mapped[list[Role]] = relationship(  # type: ignore[assignment]
        "Role", secondary=roles_users, backref=backref("users", lazy="dynamic")
    )

    organizations: Mapped[list[OrganizationMember]] = relationship(
        back_populates="user"
    )
    teams: Mapped[list[TeamMember]] = relationship(back_populates="user")

    def __repr__(self):
        return "<User {0}>".format(self.username)

    @property
    def projects(self):
        projects = []
        for organization in self.organizations:
            projects += organization.organization.projects
        for team in self.teams:
            for org_team in team.team.organizations:
                projects += org_team.organization.projects
        return projects


class GitHubIdentity(Model):
    __tablename__ = "github_identities"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id"),
        unique=True,
    )
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    github_username: Mapped[str] = mapped_column(String(255))
    github_access_token: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.now
    )

    user: Mapped[User] = relationship(backref=backref("github_identity", uselist=False))


class WebAuthn(Model, FsWebAuthnMixin):
    __tablename__ = "webauthn"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id"),
    )


class TailscaleIntegration(Model):
    __tablename__ = "tailscale_integrations"

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
    client_id: Mapped[str] = mapped_column(String(255))
    client_secret_vault_path: Mapped[str | None] = mapped_column(String(512))
    tailnet: Mapped[str | None] = mapped_column(String(255))
    default_tags: Mapped[str | None] = mapped_column(String(512))
    operator_state: Mapped[str] = mapped_column(String(32), default="pending")
    operator_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=datetime.datetime.now,
        onupdate=datetime.datetime.now,
    )

    organization: Mapped[Organization] = relationship(
        backref=backref("tailscale_integration", uselist=False),
    )

    def __repr__(self):
        return f"<TailscaleIntegration {self.id} org={self.organization_id}>"


class SlackIntegration(Model):
    __versioned__: dict = {}
    __tablename__ = "slack_integrations"

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
    team_id: Mapped[str] = mapped_column(String(64))
    team_name: Mapped[str | None] = mapped_column(String(255))
    bot_user_id: Mapped[str | None] = mapped_column(String(64))
    access_token_vault_path: Mapped[str | None] = mapped_column(String(512))
    default_channel_id: Mapped[str | None] = mapped_column(String(64))
    default_channel_name: Mapped[str | None] = mapped_column(String(255))
    installed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id"),
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=datetime.datetime.now,
        onupdate=datetime.datetime.now,
    )

    version_id: Mapped[int] = mapped_column(Integer)

    organization: Mapped[Organization] = relationship(
        backref=backref("slack_integration", uselist=False),
    )
    installed_by: Mapped[User | None] = relationship(
        foreign_keys=[installed_by_user_id]
    )

    __mapper_args__ = {"version_id_col": version_id}

    def __repr__(self):
        return f"<SlackIntegration {self.id} org={self.organization_id} team={self.team_id}>"


class DiscordIntegration(Model):
    __versioned__: dict = {}
    __tablename__ = "discord_integrations"

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
    guild_id: Mapped[str] = mapped_column(String(64))
    guild_name: Mapped[str | None] = mapped_column(String(255))
    default_channel_id: Mapped[str | None] = mapped_column(String(64))
    default_channel_name: Mapped[str | None] = mapped_column(String(255))
    installed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id"),
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=datetime.datetime.now,
        onupdate=datetime.datetime.now,
    )

    version_id: Mapped[int] = mapped_column(Integer)

    organization: Mapped[Organization] = relationship(
        backref=backref("discord_integration", uselist=False),
    )
    installed_by: Mapped[User | None] = relationship(
        foreign_keys=[installed_by_user_id]
    )

    __mapper_args__ = {"version_id_col": version_id}

    def __repr__(self):
        return f"<DiscordIntegration {self.id} org={self.organization_id} guild={self.guild_id}>"


class Organization(Model):
    __versioned__: dict = {}
    __tablename__ = "organizations"

    def __init__(self, *args, **kwargs):
        if "slug" not in kwargs:
            kwargs["slug"] = slugify(kwargs.get("name"))
        if "k8s_identifier" not in kwargs:
            kwargs["k8s_identifier"] = generate_k8s_identifier(kwargs["slug"])
        super().__init__(*args, **kwargs)

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    name: Mapped[str] = mapped_column(Text())
    slug: Mapped[str] = mapped_column(postgresql.CITEXT(), unique=True)
    k8s_identifier: Mapped[str] = mapped_column(String(64), unique=True)
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, index=True)

    members: Mapped[list[OrganizationMember]] = relationship(
        back_populates="organization"
    )
    teams: Mapped[list[OrganizationTeam]] = relationship(back_populates="organization")

    projects: Mapped[list[Project]] = relationship(back_populates="organization")

    @property
    def active_projects(self):
        return [p for p in self.projects if p.deleted_at is None]

    def add_user(self, user, admin=False):
        association = OrganizationMember(admin=admin)
        association.organization = self
        association.user = user
        db.session.add(association)

    def remove_user(self, user):
        association = OrganizationMember.query.filter_by(
            user_id=user.id, organization_id=self.id
        ).first()
        if association:
            db.session.delete(association)

    def add_team(self, team):
        association = OrganizationTeam()
        association.organization = self
        association.team = team
        db.session.add(association)


class Team(Model):
    __versioned__: dict = {}
    __tablename__ = "teams"

    def __init__(self, *args, **kwargs):
        if "slug" not in kwargs:
            kwargs["slug"] = slugify(kwargs.get("name"))
        super().__init__(*args, **kwargs)

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )

    name: Mapped[str] = mapped_column(String(64))
    slug: Mapped[str] = mapped_column(String(64))

    organizations: Mapped[list[OrganizationTeam]] = relationship(back_populates="team")
    members: Mapped[list[TeamMember]] = relationship(back_populates="team")

    def add_user(self, user, admin=False):
        association = TeamMember(admin=admin)
        association.team = self
        association.user = user
        db.session.add(association)

    def remove_user(self, user):
        association = TeamMember.query.filter_by(user_id=user.id, team_id=self.id)
        if association:
            db.session.delete(association)
