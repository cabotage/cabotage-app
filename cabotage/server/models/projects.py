
from citext import CIText
from sqlalchemy import text, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy_continuum import make_versioned
from sqlalchemy_utils.models import Timestamp

from cabotage.server import db

from .utils import slugify

make_versioned()


class Project(db.Model, Timestamp):

    __versioned__ = {}
    __tablename__ = 'projects'

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
    organization_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey('organizations.id')
    )
    name = db.Column(db.Text(), nullable=False)
    slug = db.Column(CIText(), nullable=False)

    project_applications = db.relationship(
        "Application",
        backref="project"
    )

    UniqueConstraint('organization_id', 'slug')


class Application(db.Model, Timestamp):

    __versioned__ = {}
    __tablename__ = 'project_applications'

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True
    )
    project_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey('projects.id'),
        nullable=False,
    )
    name = db.Column(db.Text(), nullable=False)
    slug = db.Column(CIText(), nullable=False)

    container = db.relationship(
        "Container",
        backref="application",
        uselist=False,
    )
    configurations = db.relationship(
        "Configuration",
        backref="application"
    )
    releases = db.relationship(
        "Release",
        backref="application"
    )

    UniqueConstraint('project_id', 'slug')


class Release(db.Model, Timestamp):

    __versioned__ = {}
    __tablename__ = 'project_app_releases'

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True
    )
    application_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey('project_applications.id'),
        nullable=False,
    )


class Configuration(db.Model, Timestamp):

    __versioned__ = {}
    __tablename__ = 'project_app_configurations'

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True
    )
    application_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey('project_applications.id'),
        nullable=False,
    )

    name = db.Column(
        CIText(),
        nullable=False,
    )
    value = db.Column(
        db.String(2048),
        nullable=False,
    )

    version_id = db.Column(
        db.Integer,
        nullable=False
    )
    deleted = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    secret = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )

    UniqueConstraint('application_id', 'name')

    __mapper_args__ = {
        "version_id_col": version_id
    }


class Container(db.Model, Timestamp):

    __versioned__ = {}
    __tablename__ = 'project_app_containers'

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True
    )
    application_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey('project_applications.id'),
        nullable=False,
    )

    container_repository = db.Column(
        db.String(256),
        nullable=False,
    )
    container_tag = db.Column(
        db.String(256),
        nullable=False,
    )
    container_image_id = db.Column(
        db.String(128),
        nullable=True,
    )

    version_id = db.Column(
        db.Integer,
        nullable=False
    )
    deleted = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )

    __mapper_args__ = {
        "version_id_col": version_id
    }

db.configure_mappers()
