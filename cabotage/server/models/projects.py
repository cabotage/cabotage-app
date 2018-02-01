import json

from citext import CIText
from sqlalchemy import text, UniqueConstraint
from sqlalchemy.event import listens_for
from sqlalchemy.dialects import postgresql
from sqlalchemy_continuum import make_versioned
from sqlalchemy_utils.models import Timestamp

from cabotage.server import db

from cabotage.server.models.plugins import ActivityPlugin
from cabotage.server.models.utils import (
    slugify,
    DictDiffer,
)

activity_plugin = ActivityPlugin()
make_versioned(plugins=[activity_plugin])

platform_version = postgresql.ENUM(
    'wind',
    'steam',
    'diesel',
    'stirling',
    'nuclear',
    'electric',
    name='platform_version',
)


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
        primary_key=True,
    )
    organization_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey('organizations.id'),
    )
    name = db.Column(db.Text(), nullable=False)
    slug = db.Column(CIText(), nullable=False)

    project_applications = db.relationship(
        "Application",
        backref="project",
        cascade="all, delete-orphan",
    )

    UniqueConstraint(organization_id, slug)


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
    platform = db.Column(platform_version, nullable=False, default='wind')

    container = db.relationship(
        "Container",
        backref="application",
        uselist=False,
        cascade="all, delete-orphan",
    )
    configurations = db.relationship(
        "Configuration",
        backref="application",
        cascade="all, delete-orphan",
    )
    release = db.relationship(
        "Release",
        backref="application",
        uselist=False,
        cascade="all, delete-orphan",
    )
    version_id = db.Column(
        db.Integer,
        nullable=False
    )

    @property
    def release_candidate(self):
        release = Release(
            application_id=self.id,
            container=self.container.asdict if self.container else {},
            configuration={c.name: c.asdict for c in self.configurations},
            platform=self.platform,
        )
        return release.asdict

    @property
    def current_release(self):
        if self.release:
            return self.release.asdict
        return {}

    @property
    def ready_for_deployment(self):
        current = self.current_release
        candidate = self.release_candidate
        configuration_diff = DictDiffer(
            candidate.get('configuration', {}),
            current.get('configuration', {}),
            ignored_keys=['id'],
        )
        container_diff = DictDiffer(
            candidate.get('container', {}),
            current.get('container', {}),
            ignored_keys=['id', 'version_id'],
        )
        return container_diff, configuration_diff

    def create_release(self):
        container_diff, configuration_diff = self.ready_for_deployment
        if self.release:
            self.release.container = self.container.asdict
            self.release.configuration = {c.name: c.asdict for c in self.configurations}
            self.release.container_changes = container_diff.asdict
            self.release.configuration = {c.name: c.asdict for c in self.configurations}
            self.release.configuration_changes = configuration_diff.asdict
            self.release.platform = self.platform
            self.release.version_id += 1
            return True
        else:
            self.release = Release(
                application_id=self.id,
                container=self.container.asdict,
                container_changes=container_diff.asdict,
                configuration={c.name: c.asdict for c in self.configurations},
                configuration_changes=configuration_diff.asdict,
                platform=self.platform,
                version_id = 1,
            )
            return True
        return False

    UniqueConstraint(project_id, slug)

    __mapper_args__ = {
        "version_id_col": version_id
    }


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
        unique=True,
        nullable=False,
    )
    platform = db.Column(platform_version, nullable=False, default='wind')
    container = db.Column(postgresql.JSONB(), nullable=False)
    configuration = db.Column(postgresql.JSONB(), nullable=False)
    container_changes = db.Column(postgresql.JSONB(), nullable=False)
    configuration_changes = db.Column(postgresql.JSONB(), nullable=False)
    version_id = db.Column(
        db.Integer,
        nullable=False
    )

    @property
    def asdict(self):
        return {
            "id": str(self.id),
            "application_id": str(self.application_id),
            "platform": self.platform,
            "container": self.container,
            "configuration": self.configuration,
        }

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
    key_slug = db.Column(
        db.Text(),
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
    secret = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )

    UniqueConstraint(application_id, name)

    __mapper_args__ = {
        "version_id_col": version_id
    }

    @property
    def asdict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "version_id": self.version_id,
            "secret": self.secret,
        }


class Image(db.Model, Timestamp):

    __versioned__ = {}
    __tablename__ = 'project_app_images'

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

    repository_name = db.Column(
        db.String(256),
        nullable=False,
    )
    image_id = db.Column(
        db.String(256),
        nullable=True,
    )
    version = db.Column(
        db.Integer,
        nullable=False,
    )

    version_id = db.Column(
        db.Integer,
        nullable=False,
    )
    built = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )
    deleted = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )

    __mapper_args__ = {
        "version_id_col": version_id
    }


@listens_for(Image, 'before_insert')
def image_before_insert_listener(mapper, connection, target):
    most_recent_image = mapper.class_.query.filter_by(application_id=target.application_id).order_by(mapper.class_.version.desc()).first()
    if most_recent_image is None:
        target.version = 1
    else:
        target.version = most_recent_image.version + 1


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
        unique=True,
        nullable=False,
    )

    container_tag = db.Column(
        db.String(256),
        nullable=False,
    )
    container_image_id = db.Column(
        db.String(256),
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

    @property
    def asdict(self):
        return {
            "id": str(self.id),
            "container_image_id": self.container_image_id,
            "version_id": self.version_id,
        }
