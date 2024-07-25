import datetime


from flask_security.models.fsqla_v3 import FsRoleMixin, FsUserMixin


from cabotage.server import db
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy_continuum import make_versioned

from citext import CIText

from .auth_associations import (
    OrganizationMember,
    OrganizationTeam,
    TeamMember,
)

from cabotage.server.models.plugins import ActivityPlugin
from cabotage.server.models.utils import slugify

activity_plugin = ActivityPlugin()
make_versioned(plugins=[activity_plugin])


roles_users = db.Table(
    "roles_users",
    db.Column("user_id", postgresql.UUID(as_uuid=True), db.ForeignKey("users.id")),
    db.Column("role_id", postgresql.UUID(as_uuid=True), db.ForeignKey("roles.id")),
)


class Role(db.Model, FsRoleMixin):
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


class User(db.Model, FsUserMixin):
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

    roles = db.relationship(
        "Role", secondary=roles_users, backref=db.backref("users", lazy="dynamic")
    )

    organizations = db.relationship("OrganizationMember", back_populates="user")
    teams = db.relationship("TeamMember", back_populates="user")

    def is_authenticated(self):
        return True

    def is_active(self):
        return self.active

    def is_anonymous(self):
        return False

    def get_id(self):
        return self.id

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


class Organization(db.Model):
    __versioned__: dict = {}
    __tablename__ = "organizations"

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
    name = db.Column(db.Text(), nullable=False)
    slug = db.Column(CIText(), nullable=False, unique=True)

    members = db.relationship("OrganizationMember", back_populates="organization")
    teams = db.relationship("OrganizationTeam", back_populates="organization")

    projects = db.relationship("Project", backref="organization")

    def add_user(self, user, admin=False):
        association = OrganizationMember(admin=admin)
        association.organization = self
        association.user = user
        db.session.add(association)

    def remove_user(self, user):
        association = OrganizationMember.query.filter_by(
            user_id=user.id, organization_id=self.id
        )
        if association:
            db.session.delete(association)

    def add_team(self, team):
        association = OrganizationTeam()
        association.organization = self
        association.team = team
        db.session.add(association)


class Team(db.Model):
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
