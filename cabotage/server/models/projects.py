import re

from cabotage.server import db
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from unidecode import unidecode


_punct_re = re.compile(r'[\t !"#$%&\'()*\-/<=>?@\[\\\]^_`{|},.]+')


def slugify(text, delim=u'-'):
    """Generates an ASCII-only slug."""
    result = []
    for word in _punct_re.split(text.lower()):
        result.extend(unidecode(word).split())
    return str(delim.join(result))


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
    name = db.Column(db.String(64), nullable=False)
    slug = db.Column(db.String(64), nullable=False)

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
    name = db.Column(db.String(64), nullable=False)
    slug = db.Column(db.String(64), nullable=False)

    project = db.relationship(
        "Project",
        back_populates="pipelines"
    )
    applications = db.relationship(
        "Application",
        back_populates="pipeline"
    )


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
    name = db.Column(db.String(64), nullable=False)
    slug = db.Column(db.String(64), nullable=False)

    project = db.relationship(
        "Project",
        back_populates="project_applications"
    )
    pipeline = db.relationship(
        "Pipeline",
        back_populates="applications"
    )
    releases = db.relationship(
        "Release",
        back_populates="application"
    )


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
