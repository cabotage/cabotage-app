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
        DateTime,
        default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ),
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
        DateTime,
        default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ),
    )

    user: Mapped[User] = relationship(backref=backref("github_identity", uselist=False))


class GitHubAppInstallation(Model):
    __tablename__ = "github_app_installations"

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
    installation_id: Mapped[int] = mapped_column(BigInteger, index=True)
    account_id: Mapped[int | None] = mapped_column(BigInteger)
    account_login: Mapped[str | None] = mapped_column(String(255))
    account_type: Mapped[str | None] = mapped_column(String(32))
    repository_selection: Mapped[str | None] = mapped_column(String(32))
    repositories: Mapped[list[dict[str, object]] | None] = mapped_column(
        postgresql.JSONB()
    )
    repositories_synced_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    installed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id"),
    )
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

    organization: Mapped[Organization] = relationship(
        back_populates="github_app_installations"
    )
    installed_by: Mapped[User | None] = relationship(
        foreign_keys=[installed_by_user_id]
    )

    @property
    def display_name(self):
        return self.account_login or "Unknown account"

    @property
    def github_settings_url(self):
        if self.account_type == "Organization" and self.account_login:
            return (
                f"https://github.com/organizations/{self.account_login}"
                f"/settings/installations/{self.installation_id}"
            )
        return f"https://github.com/settings/installations/{self.installation_id}"

    __table_args__ = (
        db.UniqueConstraint(
            "organization_id",
            "installation_id",
            name="uq_github_app_installations_org_installation",
        ),
    )


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

    organization: Mapped[Organization] = relationship(
        back_populates="tailscale_integration",
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

    organization: Mapped[Organization] = relationship(
        back_populates="slack_integration",
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

    organization: Mapped[Organization] = relationship(
        back_populates="discord_integration",
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

    tailscale_integration: Mapped[TailscaleIntegration | None] = relationship(
        back_populates="organization", uselist=False
    )
    slack_integration: Mapped[SlackIntegration | None] = relationship(
        back_populates="organization", uselist=False
    )
    discord_integration: Mapped[DiscordIntegration | None] = relationship(
        back_populates="organization", uselist=False
    )
    github_app_installations: Mapped[list[GitHubAppInstallation]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
        order_by="GitHubAppInstallation.account_login",
    )

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


class OrganizationRequest(Model):
    __tablename__ = "organization_requests"

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_DENIED = "denied"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id"),
        index=True,
    )
    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id"),
        index=True,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        index=True,
    )
    name: Mapped[str] = mapped_column(Text())
    slug: Mapped[str] = mapped_column(postgresql.CITEXT(), index=True)
    note: Mapped[str | None] = mapped_column(Text())
    status: Mapped[str] = mapped_column(String(32), default=STATUS_PENDING, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ),
        index=True,
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
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)

    requester: Mapped[User] = relationship(
        foreign_keys=[requester_user_id],
        backref=backref("organization_requests", lazy="dynamic"),
    )
    reviewer: Mapped[User | None] = relationship(foreign_keys=[reviewer_user_id])
    organization: Mapped[Organization | None] = relationship()

    @property
    def is_pending(self):
        return self.status == self.STATUS_PENDING

    def __repr__(self):
        return f"<OrganizationRequest {self.slug} {self.status}>"


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
