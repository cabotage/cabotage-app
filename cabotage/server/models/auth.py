import datetime
import re

from flask import current_app

from cabotage.server import db, bcrypt
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from unidecode import unidecode

from .auth_associations import (
    OrganizationMember,
    OrganizationTeam,
    TeamMember,
)

_punct_re = re.compile(r'[\t !"#$%&\'()*\-/<=>?@\[\\\]^_`{|},.]+')


def slugify(text, delim=u'-'):
    """Generates an ASCII-only slug."""
    result = []
    for word in _punct_re.split(text.lower()):
        result.extend(unidecode(word).split())
    return str(delim.join(result))


class User(db.Model):

    __tablename__ = 'users'

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True
    )
    username = db.Column(db.String(255), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    registered_on = db.Column(db.DateTime, nullable=False)
    admin = db.Column(db.Boolean, nullable=False, default=False)

    organizations = db.relationship("OrganizationMember", back_populates="user")
    teams = db.relationship("TeamMember", back_populates="user")

    def __init__(self, username, email, password, admin=False):
        self.username = username
        self.email = email
        self.password = bcrypt.generate_password_hash(
            password, current_app.config.get('BCRYPT_LOG_ROUNDS')
        ).decode('utf-8')
        self.registered_on = datetime.datetime.now()
        self.admin = admin

    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False

    def get_id(self):
        return self.id

    def __repr__(self):
        return '<User {0}>'.format(self.username)


class Organization(db.Model):

    __tablename__ = 'organizations'

    def __init__(self, *args, **kwargs):
        if 'slug' not in kwargs:
            kwargs['slug'] = slugify(kwargs.get('name'))
        super().__init__(*args, **kwargs)

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True
    )
    name = db.Column(db.String(64), nullable=False)
    slug = db.Column(db.String(64), nullable=False)

    members = db.relationship("OrganizationMember", back_populates="organization")
    teams = db.relationship("OrganizationTeam", back_populates="organization")

    projects = db.relationship("Project", back_populates="organization")

    def add_user(self, user, admin=False):
        association = OrganizationMember(admin=admin)
        association.organization = self
        association.user = user
        db.session.add(association)

    def remove_user(self, user):
        association = OrganizationMember.query.filter_by(user_id=user.id, organization_id=self.id)
        if association:
            db.session.delete(association)

    def add_team(self, team):
        association = OrganizationTeam()
        association.organization = self
        association.team = team
        db.session.add(association)


class Team(db.Model):

    __tablename__ = 'teams'

    def __init__(self, *args, **kwargs):
        if 'slug' not in kwargs:
            kwargs['slug'] = slugify(kwargs.get('name'))
        super().__init__(*args, **kwargs)

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True
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
