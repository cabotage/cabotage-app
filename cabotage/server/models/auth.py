import datetime
from enum import StrEnum
from uuid import UUID

from citext import CIText
from flask_security.models.fsqla_v3 import (
    FsModels,
    FsRoleMixin,
    FsUserMixin,
    FsWebAuthnMixin,
)
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy_continuum import make_versioned

from cabotage.server import db, Model
from cabotage.server.models.plugins import ActivityPlugin
from cabotage.server.models.utils import generate_k8s_identifier, slugify

from .auth_associations import (
    OrganizationMember,
    OrganizationTeam,
    TeamMember,
)

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

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    name = db.Column(db.String(80), unique=True)
    description = db.Column(db.String(255))

    def __str__(self):
        return self.name

    def __hash__(self):
        return hash(self.name)


class User(Model, FsUserMixin):
    __versioned__: dict = {
        "exclude": ["password"],
    }
    __tablename__ = "users"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    username = db.Column(db.String(255), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)

    admin = db.Column(db.Boolean, nullable=False, default=False)
    registered_at = db.Column(
        db.DateTime, nullable=False, default=datetime.datetime.now
    )

    roles = db.relationship(  # type: ignore[assignment]
        "Role", secondary=roles_users, backref=db.backref("users", lazy="dynamic")
    )

    organizations = db.relationship("OrganizationMember", back_populates="user")
    teams = db.relationship("TeamMember", back_populates="user")

    def __repr__(self):
        return "<User {0}>".format(self.username)

    @property
    def projects(self):
        projects = []
        for organization in self.organizations:
            projects += organization.organization.projects
        for team in self.teams:
            projects += team.team.projects
        return projects


class GitHubIdentity(Model):
    __tablename__ = "github_identities"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    user_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("users.id"),
        nullable=False,
        unique=True,
    )
    github_id = db.Column(db.BigInteger, nullable=False, unique=True)
    github_username = db.Column(db.String(255), nullable=False)
    github_access_token = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.now)

    user = db.relationship("User", backref=db.backref("github_identity", uselist=False))


class WebAuthn(Model, FsWebAuthnMixin):
    __tablename__ = "webauthn"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    user_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("users.id"),
        nullable=False,
    )


class TailscaleIntegration(Model):
    __tablename__ = "tailscale_integrations"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    organization_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("organizations.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    client_id = db.Column(db.String(255), nullable=False)
    client_secret_vault_path = db.Column(db.String(512), nullable=True)
    tailnet = db.Column(db.String(255), nullable=True)
    default_tags = db.Column(db.String(512), nullable=True)
    operator_state = db.Column(db.String(32), default="pending", nullable=False)
    operator_version = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.datetime.now,
        onupdate=datetime.datetime.now,
    )

    organization = db.relationship(
        "Organization",
        backref=db.backref("tailscale_integration", uselist=False),
    )

    def __repr__(self):
        return f"<TailscaleIntegration {self.id} org={self.organization_id}>"


class Organization(Model):
    __versioned__: dict = {}
    __tablename__ = "organizations"

    def __init__(self, *args, **kwargs):
        if "slug" not in kwargs:
            kwargs["slug"] = slugify(kwargs.get("name"))
        if "k8s_identifier" not in kwargs:
            kwargs["k8s_identifier"] = generate_k8s_identifier(kwargs["slug"])
        super().__init__(*args, **kwargs)

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    name = db.Column(db.Text(), nullable=False)
    slug = db.Column(CIText(), nullable=False, unique=True)
    k8s_identifier = db.Column(db.String(64), unique=True, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)

    members = db.relationship("OrganizationMember", back_populates="organization")
    teams = db.relationship("OrganizationTeam", back_populates="organization")
    billing = db.relationship("OrganizationBilling", back_populates="organization")

    projects = db.relationship("Project", backref="organization")

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

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )

    name = db.Column(db.String(64), nullable=False)
    slug = db.Column(db.String(64), nullable=False)

    organizations = db.relationship("OrganizationTeam", back_populates="team")
    members = db.relationship("TeamMember", back_populates="team")

    def add_user(self, user, admin=False):
        association = TeamMember(admin=admin)
        association.team = self
        association.user = user
        db.session.add(association)

    def remove_user(self, user):
        association = TeamMember.query.filter_by(user_id=user.id, team_id=self.id)
        if association:
            db.session.delete(association)


class BillingSubsctriptionStatus(StrEnum):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    """not sure what else we will want here yet"""


class Billing(Model):
    __versioned__: dict = {}
    __tablename__ = "billing"

    stripe_customer_id: Mapped[str] = mapped_column(
        db.String, nullable=True, unique=True, index=True
    )
    stripe_sub_id: Mapped[str] = mapped_column(
        db.String, nullable=True, unique=True, index=True
    )
    stripe_sub_status: Mapped[BillingSubsctriptionStatus | None] = mapped_column(
        db.String, nullable=True
    )
    stripe_sub_plan: Mapped[str] = mapped_column(db.String, default="free")


class BillingWebhookEvent(Model):
    __versioned__: dict = {}
    __tablename__ = "billing_webhook_events"

    id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    stripe_event_id: Mapped[str] = mapped_column(unique=True, index=True)
    event_type: Mapped[str] = mapped_column()
    processed_at: Mapped[datetime.datetime] = mapped_column(
        server_default=text("now()")
    )
    payload: Mapped[dict] = mapped_column(JSONB)
