
from cabotage.server import db
from sqlalchemy import text, UniqueConstraint
from sqlalchemy.dialects import postgresql
from citext import CIText

from .utils import slugify


class Project(db.Model):

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

    organization = db.relationship(
        "Organization",
        back_populates="projects"
    )

    project_applications = db.relationship(
        "Application",
        back_populates="project"
    )
    pipeline_applications = db.relationship(
        "Pipeline",
        back_populates="project"
    )
    pipelines = db.relationship(
        "Pipeline",
        back_populates="project"
    )

    UniqueConstraint('organization_id', 'slug')


class Pipeline(db.Model):

    __tablename__ = 'project_pipelines'

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True
    )
    project_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey('projects.id')
    )
    name = db.Column(db.Text(), nullable=False)
    slug = db.Column(CIText(), nullable=False)

    project = db.relationship(
        "Project",
        back_populates="pipelines"
    )
    applications = db.relationship(
        "Application",
        back_populates="pipeline"
    )

    UniqueConstraint('project_id', 'slug')


class Application(db.Model):

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
    project_pipeline_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey('project_pipelines.id')
    )
    name = db.Column(db.Text(), nullable=False)
    slug = db.Column(CIText(), nullable=False)

    project = db.relationship(
        "Project",
        back_populates="project_applications"
    )
    pipeline = db.relationship(
        "Pipeline",
        back_populates="applications"
    )

    container = db.relationship(
        "Container",
        back_populates="application"
    )
    configurations = db.relationship(
        "Configuration",
        back_populates="application"
    )
    releases = db.relationship(
        "Release",
        back_populates="application"
    )

    UniqueConstraint('project_id', 'slug')


class Release(db.Model):

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

    application = db.relationship(
        "Application",
        back_populates="releases"
    )


class Configuration(db.Model):

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

    application = db.relationship(
        "Application",
        back_populates="configurations"
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


class Container(db.Model):

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

    application = db.relationship(
        "Application",
        back_populates="container"
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
