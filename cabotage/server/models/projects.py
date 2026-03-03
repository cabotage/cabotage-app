import json

from citext import CIText
from flask import current_app
from sqlalchemy import CheckConstraint, text, UniqueConstraint
from sqlalchemy.event import listens_for
from sqlalchemy.dialects import postgresql
from sqlalchemy_continuum import make_versioned
from sqlalchemy_utils.models import Timestamp

from cabotage.server import db

from cabotage.server.models.plugins import ActivityPlugin
from cabotage.server.models.utils import (
    generate_k8s_identifier,
    slugify,
    DictDiffer,
)
from cabotage.utils.docker_auth import (
    generate_docker_credentials,
    generate_kubernetes_imagepullsecrets,
)
from cabotage.utils.release_build_context import (
    configmap_context_for_release,
    RELEASE_DOCKERFILE_TEMPLATE,
)

activity_plugin = ActivityPlugin()
make_versioned(plugins=[activity_plugin])

platform_version = postgresql.ENUM(
    "wind",
    "steam",
    "diesel",
    "stirling",
    "nuclear",
    "electric",
    name="platform_version",
)

pod_classes = {
    "m1.small": {
        "cpu": {"requests": "125m", "limits": "250m"},
        "memory": {"requests": "256Mi", "limits": "384Mi"},
    },
    "m1.medium": {
        "cpu": {"requests": "250m", "limits": "500m"},
        "memory": {"requests": "512Mi", "limits": "768Mi"},
    },
    "m1.large": {
        "cpu": {"requests": "500m", "limits": "1000m"},
        "memory": {"requests": "1024Mi", "limits": "1536Mi"},
    },
    "c1.small": {
        "cpu": {"requests": "250m", "limits": "375m"},
        "memory": {"requests": "256Mi", "limits": "384Mi"},
    },
    "c1.medium": {
        "cpu": {"requests": "500m", "limits": "750m"},
        "memory": {"requests": "512Mi", "limits": "768Mi"},
    },
    "c1.large": {
        "cpu": {"requests": "1000m", "limits": "1500m"},
        "memory": {"requests": "1024Mi", "limits": "1536Mi"},
    },
    "r1.small": {
        "cpu": {"requests": "125m", "limits": "250m"},
        "memory": {"requests": "1024Mi", "limits": "1536Mi"},
    },
    "r1.medium": {
        "cpu": {"requests": "250m", "limits": "500m"},
        "memory": {"requests": "1536Mi", "limits": "2304Mi"},
    },
    "r1.large": {
        "cpu": {"requests": "500m", "limits": "1000m"},
        "memory": {"requests": "2048Mi", "limits": "3072Mi"},
    },
    "r1.xlarge": {
        "cpu": {"requests": "750m", "limits": "1500m"},
        "memory": {"requests": "2048Mi", "limits": "3072Mi"},
    },
    "r1.2xlarge": {
        "cpu": {"requests": "1500m", "limits": "1500m"},
        "memory": {"requests": "4096Mi", "limits": "4096Mi"},
    },
}

DEFAULT_POD_CLASS = "m1.large"


class Project(db.Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "projects"

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
    organization_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("organizations.id"),
    )
    name = db.Column(db.Text(), nullable=False)
    slug = db.Column(CIText(), nullable=False)
    k8s_identifier = db.Column(db.String(64), nullable=False)
    environments_enabled = db.Column(
        db.Boolean, nullable=False, default=False, server_default="false"
    )

    project_applications = db.relationship(
        "Application",
        backref="project",
        cascade="all, delete-orphan",
    )
    project_environments = db.relationship(
        "Environment",
        backref="project",
        cascade="all, delete-orphan",
        order_by="Environment.sort_order",
    )

    __table_args__ = (
        UniqueConstraint(organization_id, slug),
        UniqueConstraint(organization_id, k8s_identifier),
    )


class Environment(db.Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "project_environments"

    def __init__(self, *args, **kwargs):
        if "slug" not in kwargs and "name" in kwargs:
            kwargs["slug"] = slugify(kwargs["name"])
        if "k8s_identifier" not in kwargs and "slug" in kwargs:
            kwargs["k8s_identifier"] = generate_k8s_identifier(kwargs["slug"])
        super().__init__(*args, **kwargs)

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    project_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("projects.id"),
        nullable=False,
    )
    name = db.Column(db.Text(), nullable=False)
    slug = db.Column(CIText(), nullable=False)
    k8s_identifier = db.Column(db.String(64), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=100)
    ephemeral = db.Column(db.Boolean, nullable=False, default=False)
    ttl_hours = db.Column(db.Integer, nullable=True)
    is_default = db.Column(db.Boolean, nullable=False, default=False)
    version_id = db.Column(db.Integer, nullable=False)

    application_environments = db.relationship(
        "ApplicationEnvironment",
        backref="environment",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(project_id, slug),
        UniqueConstraint(project_id, k8s_identifier),
    )

    __mapper_args__ = {"version_id_col": version_id}


class ApplicationEnvironment(db.Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "application_environments"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    application_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("project_applications.id"),
        nullable=False,
    )
    environment_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("project_environments.id"),
        nullable=False,
    )
    process_counts = db.Column(
        postgresql.JSONB(), server_default=text("json_object('{}')")
    )
    process_pod_classes = db.Column(
        postgresql.JSONB(), server_default=text("json_object('{}')")
    )
    deployment_timeout = db.Column(
        db.Integer,
        nullable=True,
    )
    health_check_path = db.Column(
        db.String(64),
        nullable=True,
    )
    health_check_host = db.Column(
        db.String(256),
        nullable=True,
    )
    auto_deploy_branch = db.Column(
        db.Text(),
        nullable=True,
    )
    github_environment_name = db.Column(
        db.Text(),
        nullable=True,
    )
    version_id = db.Column(db.Integer, nullable=False)

    configurations = db.relationship(
        "Configuration",
        backref="application_environment",
        foreign_keys="Configuration.application_environment_id",
    )
    images = db.relationship(
        "Image",
        backref="application_environment",
        foreign_keys="Image.application_environment_id",
        lazy="dynamic",
    )
    releases = db.relationship(
        "Release",
        backref="application_environment",
        foreign_keys="Release.application_environment_id",
        lazy="dynamic",
    )
    deployments = db.relationship(
        "Deployment",
        backref="application_environment",
        foreign_keys="Deployment.application_environment_id",
        lazy="dynamic",
    )

    __table_args__ = (
        UniqueConstraint(application_id, environment_id),
    )

    __mapper_args__ = {"version_id_col": version_id}

    @property
    def latest_image(self):
        return self.images.filter_by().order_by(Image.version.desc()).first()

    @property
    def latest_image_built(self):
        return self.images.filter_by(built=True).order_by(Image.version.desc()).first()

    @property
    def latest_release(self):
        return self.releases.filter_by().order_by(Release.version.desc()).first()

    @property
    def latest_release_built(self):
        return (
            self.releases.filter_by(built=True).order_by(Release.version.desc()).first()
        )

    @property
    def latest_image_error(self):
        return self.images.filter_by(error=True).order_by(Image.version.desc()).first()

    @property
    def latest_image_building(self):
        return (
            self.images.filter_by(built=False, error=False)
            .order_by(Image.version.desc())
            .first()
        )

    @property
    def latest_release_error(self):
        return (
            self.releases.filter_by(error=True).order_by(Release.version.desc()).first()
        )

    @property
    def latest_release_building(self):
        return (
            self.releases.filter_by(built=False, error=False)
            .order_by(Release.version.desc())
            .first()
        )

    @property
    def latest_deployment(self):
        return self.deployments.filter_by().order_by(Deployment.created.desc()).first()

    @property
    def latest_deployment_completed(self):
        return (
            self.deployments.filter_by(complete=True)
            .order_by(Deployment.created.desc())
            .first()
        )

    @property
    def ready_for_deployment(self):
        return self.application.ready_for_deployment_in_env(self)

    @property
    def effective_auto_deploy_branch(self):
        return self.auto_deploy_branch or self.application.auto_deploy_branch

    @property
    def effective_github_environment_name(self):
        if self.github_environment_name is not None:
            return self.github_environment_name
        return f"{self.application.project.slug}/{self.environment.slug}/{self.application.slug}"

    @property
    def effective_deployment_timeout(self):
        if self.deployment_timeout is not None:
            return self.deployment_timeout
        return self.application.deployment_timeout

    @property
    def effective_health_check_path(self):
        return self.health_check_path or self.application.health_check_path

    @property
    def effective_health_check_host(self):
        return self.health_check_host or self.application.health_check_host


class Application(db.Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "project_applications"

    def __init__(self, *args, **kwargs):
        if "slug" in kwargs and "k8s_identifier" not in kwargs:
            kwargs["k8s_identifier"] = generate_k8s_identifier(kwargs["slug"])
        super().__init__(*args, **kwargs)

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    project_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("projects.id"),
        nullable=False,
    )
    name = db.Column(db.Text(), nullable=False)
    slug = db.Column(CIText(), nullable=False)
    k8s_identifier = db.Column(db.String(64), nullable=False)
    platform = db.Column(platform_version, nullable=False, default="wind")
    process_counts = db.Column(
        postgresql.JSONB(), server_default=text("json_object('{}')")
    )
    process_pod_classes = db.Column(
        postgresql.JSONB(), server_default=text("json_object('{}')")
    )

    images = db.relationship(
        "Image",
        backref="application",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    configurations = db.relationship(
        "Configuration",
        backref="application",
        cascade="all, delete-orphan",
        order_by="Configuration.name",
    )
    releases = db.relationship(
        "Release",
        backref="application",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    deployments = db.relationship(
        "Deployment",
        backref="application",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    application_environments = db.relationship(
        "ApplicationEnvironment",
        backref="application",
        cascade="all, delete-orphan",
    )
    version_id = db.Column(db.Integer, nullable=False)

    github_app_installation_id = db.Column(
        db.Integer,
        nullable=True,
    )
    github_repository = db.Column(
        db.Text(),
        nullable=True,
    )
    github_repository_is_private = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
    )
    github_environment_name = db.Column(
        db.Text(),
        nullable=True,
    )

    subdirectory = db.Column(
        db.Text(),
        nullable=True,
    )

    dockerfile_path = db.Column(
        db.Text(),
        nullable=True,
    )

    auto_deploy_branch = db.Column(
        db.Text(),
        nullable=True,
    )
    deployment_timeout = db.Column(
        db.Integer,
        nullable=True,
        server_default="180",
    )

    health_check_path = db.Column(
        db.String(64),
        nullable=False,
        server_default="/_health/",
    )
    health_check_host = db.Column(
        db.String(256),
        nullable=True,
        server_default=None,
    )

    privileged = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
    )

    @property
    def release_candidate(self):
        release = Release(
            application_id=self.id,
            image=self.latest_image_built.asdict if self.latest_image_built else {},
            configuration={c.name: c.asdict for c in self.configurations},
            platform=self.platform,
        )
        return release.asdict

    @property
    def latest_release(self):
        return self.releases.filter_by(
            application_environment_id=None,
        ).order_by(Release.version.desc()).first()

    @property
    def latest_release_built(self):
        return (
            self.releases.filter_by(
                built=True, application_environment_id=None,
            ).order_by(Release.version.desc()).first()
        )

    @property
    def latest_release_error(self):
        return (
            self.releases.filter_by(
                error=True, application_environment_id=None,
            ).order_by(Release.version.desc()).first()
        )

    @property
    def latest_release_building(self):
        return (
            self.releases.filter_by(
                built=False, error=False, application_environment_id=None,
            )
            .order_by(Release.version.desc())
            .first()
        )

    @property
    def current_release(self):
        if self.latest_release:
            return self.latest_release.asdict
        return {}

    @property
    def latest_deployment(self):
        return self.deployments.filter_by(
            application_environment_id=None,
        ).order_by(Deployment.created.desc()).first()

    @property
    def latest_deployment_completed(self):
        return (
            self.deployments.filter_by(
                complete=True, application_environment_id=None,
            )
            .order_by(Deployment.created.desc())
            .first()
        )

    @property
    def latest_deployment_error(self):
        return (
            self.deployments.filter_by(
                error=True, application_environment_id=None,
            )
            .order_by(Deployment.created.desc())
            .first()
        )

    @property
    def latest_deployment_running(self):
        return (
            self.deployments.filter_by(
                complete=False, error=False, application_environment_id=None,
            )
            .order_by(Deployment.created.desc())
            .first()
        )

    @property
    def current_deployment(self):
        if self.latest_deployment:
            return self.latest_deployment.asdict
        return {}

    @property
    def recent_deployments(self):
        return self.deployments.filter_by(
            application_environment_id=None,
        ).order_by(Deployment.created.desc()).limit(5)

    @property
    def ready_for_deployment(self):
        current = self.current_release
        candidate = self.release_candidate
        configuration_diff = DictDiffer(
            candidate.get("configuration", {}),
            current.get("configuration", {}),
            ignored_keys=["id", "version_id"],
        )
        image_diff = DictDiffer(
            candidate.get("image", {}),
            current.get("image", {}),
            ignored_keys=["id", "version_id"],
        )
        return image_diff, configuration_diff

    def release_candidate_for_env(self, app_env):
        release = Release(
            application_id=self.id,
            application_environment_id=app_env.id,
            image=app_env.latest_image_built.asdict if app_env.latest_image_built else {},
            configuration={c.name: c.asdict for c in app_env.configurations},
            platform=self.platform,
        )
        return release.asdict

    def ready_for_deployment_in_env(self, app_env):
        current = {}
        if app_env.latest_release:
            current = app_env.latest_release.asdict
        candidate = self.release_candidate_for_env(app_env)
        configuration_diff = DictDiffer(
            candidate.get("configuration", {}),
            current.get("configuration", {}),
            ignored_keys=["id", "version_id"],
        )
        image_diff = DictDiffer(
            candidate.get("image", {}),
            current.get("image", {}),
            ignored_keys=["id", "version_id"],
        )
        return image_diff, configuration_diff

    def registry_repository_name(self, app_env=None):
        """Build the registry repo name using k8s identifiers."""
        org_k8s = self.project.organization.k8s_identifier
        project_k8s = self.project.k8s_identifier
        app_k8s = self.k8s_identifier
        env_k8s = None
        if app_env is not None:
            env_k8s = app_env.environment.k8s_identifier
        return Image._build_repository_name(org_k8s, project_k8s, app_k8s, env_k8s)

    def create_release(self, app_env=None):
        if app_env is not None:
            return self._create_release_for_env(app_env)
        image_diff, configuration_diff = self.ready_for_deployment
        release = Release(
            application_id=self.id,
            image=self.latest_image.asdict,
            _repository_name=self.registry_repository_name(),
            configuration={c.name: c.asdict for c in self.configurations},
            image_changes=image_diff.asdict,
            configuration_changes=configuration_diff.asdict,
            platform=self.platform,
            health_check_path=self.health_check_path,
            health_check_host=self.health_check_host,
        )
        return release

    def _create_release_for_env(self, app_env):
        image_diff, configuration_diff = self.ready_for_deployment_in_env(app_env)
        release = Release(
            application_id=self.id,
            application_environment_id=app_env.id,
            image=app_env.latest_image_built.asdict if app_env.latest_image_built else {},
            _repository_name=self.registry_repository_name(app_env),
            configuration={c.name: c.asdict for c in app_env.configurations},
            image_changes=image_diff.asdict,
            configuration_changes=configuration_diff.asdict,
            platform=self.platform,
            health_check_path=app_env.effective_health_check_path,
            health_check_host=app_env.effective_health_check_host,
        )
        return release

    @property
    def latest_image(self):
        return self.images.filter_by(
            application_environment_id=None,
        ).order_by(Image.version.desc()).first()

    @property
    def latest_image_built(self):
        return self.images.filter_by(
            built=True, application_environment_id=None,
        ).order_by(Image.version.desc()).first()

    @property
    def latest_image_error(self):
        return self.images.filter_by(
            error=True, application_environment_id=None,
        ).order_by(Image.version.desc()).first()

    @property
    def latest_image_building(self):
        return (
            self.images.filter_by(
                built=False, error=False, application_environment_id=None,
            )
            .order_by(Image.version.desc())
            .first()
        )

    __table_args__ = (
        UniqueConstraint(project_id, slug),
        UniqueConstraint(project_id, k8s_identifier),
        db.Index(
            "github_deployments_unique",
            github_app_installation_id,
            github_repository,
            github_environment_name,
            unique=True,
            postgresql_where=(github_environment_name is not None),
        ),
    )

    __mapper_args__ = {"version_id_col": version_id}


class Deployment(db.Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "deployments"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    application_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("project_applications.id"),
        nullable=False,
    )
    application_environment_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("application_environments.id"),
        nullable=True,
    )
    release = db.Column(postgresql.JSONB(), nullable=False)
    version_id = db.Column(db.Integer, nullable=False)
    complete = db.Column(db.Boolean, nullable=False, default=False)
    error = db.Column(db.Boolean, nullable=False, default=False)
    error_detail = db.Column(
        db.String(2048),
        nullable=True,
    )
    deploy_metadata = db.Column(
        postgresql.JSONB(),
        nullable=True,
    )
    deploy_log = db.Column(
        db.Text(),
        nullable=True,
    )
    job_id = db.Column(
        db.String(64),
        nullable=True,
    )

    __mapper_args__ = {"version_id_col": version_id}

    @property
    def release_object(self):
        return Release.query.filter_by(id=self.release.get("id", None)).first()


class Release(db.Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "project_app_releases"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    application_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("project_applications.id"),
        nullable=False,
    )
    application_environment_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("application_environments.id"),
        nullable=True,
    )
    platform = db.Column(platform_version, nullable=False, default="wind")
    image = db.Column(postgresql.JSONB(), nullable=False)
    configuration = db.Column(postgresql.JSONB(), nullable=False)
    image_changes = db.Column(postgresql.JSONB(), nullable=False)
    configuration_changes = db.Column(postgresql.JSONB(), nullable=False)
    version_id = db.Column(db.Integer, nullable=False)

    _repository_name = db.Column(
        "repository_name",
        db.String(256),
        nullable=False,
    )
    release_id = db.Column(
        db.String(256),
        nullable=True,
    )
    version = db.Column(
        db.Integer,
        nullable=False,
    )

    built = db.Column(db.Boolean, nullable=False, default=False)
    error = db.Column(db.Boolean, nullable=False, default=False)
    error_detail = db.Column(
        db.String(2048),
        nullable=True,
    )
    deleted = db.Column(db.Boolean, nullable=False, default=False)
    dockerfile = db.Column(
        db.Text(),
        nullable=True,
    )
    release_metadata = db.Column(
        postgresql.JSONB(),
        nullable=True,
    )
    release_build_log = db.Column(
        db.Text(),
        nullable=True,
    )
    build_job_id = db.Column(
        db.String(64),
        nullable=True,
    )
    health_check_path = db.Column(
        db.String(64),
        nullable=False,
        server_default="/_health/",
    )
    health_check_host = db.Column(
        db.String(256),
        nullable=True,
        server_default=None,
    )

    __mapper_args__ = {"version_id_col": version_id}

    @property
    def valid(self):
        return (self.image_object is not None) and all(
            v is not None for v in self.configuration_objects.values()
        )

    @property
    def deposed(self):
        return not self.valid

    @property
    def deposed_reason(self):
        reasons = []
        if self.image_object is None:
            reasons.append(
                f'<code>Image {self.image["repository"]}:{self.image["tag"]} '
                "no longer exists!</code>"
            )
        for configuration, configuration_serialized in self.configuration.items():
            configuration_object = Configuration.query.filter_by(
                id=configuration_serialized["id"]
            ).first()
            if configuration_object is None:
                reasons.append(
                    f"<code>Configuration for {configuration} no longer exists!</code>"
                )
        return reasons

    @property
    def asdict(self):
        return {
            "id": str(self.id),
            "application_id": str(self.application_id),
            "platform": self.platform,
            "image": self.image,
            "configuration": self.configuration,
        }

    @property
    def configuration_objects(self):
        return {
            k: Configuration.query.filter_by(id=v["id"]).first()
            for k, v in self.configuration.items()
        }

    @property
    def envconsul_configurations(self):
        configurations = {}
        environment_statements = "\n".join(
            [
                c.envconsul_statement
                for c in self.configuration_objects.values()
                if c is not None
            ]
        )
        exec_statement = "exec {\n" '  command = "/bin/sh"\n'
        if not self.application.privileged:
            exec_statement += (
                "  env = {\n"
                '    denylist = ["CONSUL_*", "VAULT_*", "KUBERNETES_*"]\n'
                "  }\n"
            )
        exec_statement += "}"
        configurations["shell"] = "\n".join([exec_statement, environment_statements])
        for proc_name, proc in self.image_object.processes.items():
            custom_env = json.dumps([f"{key}={value}" for key, value in proc["env"]])
            exec_statement = "exec {\n" f'  command = {json.dumps(proc["cmd"])}\n'
            if not self.application.privileged:
                exec_statement += (
                    "  env = {\n"
                    f"    custom = {custom_env}\n"
                    '    denylist = ["CONSUL_*", "VAULT_*", "KUBERNETES_*"]\n'
                    "  }\n"
                )
            exec_statement += "}"
            configurations[proc_name] = "\n".join(
                [exec_statement, environment_statements]
            )
        return configurations

    @property
    def image_object(self):
        return Image.query.filter_by(id=self.image.get("id", None)).first()

    @property
    def processes(self):
        return {
            k: v
            for k, v in self.image_object.processes.items()
            if not (k.startswith("release") or k.startswith("postdeploy"))
        }

    @property
    def release_commands(self):
        return {
            k: v
            for k, v in self.image_object.processes.items()
            if k.startswith("release")
        }

    @property
    def postdeploy_commands(self):
        return {
            k: v
            for k, v in self.image_object.processes.items()
            if k.startswith("postdeploy")
        }

    @property
    def repository_name(self):
        app_env = self.application_environment if self.application_environment_id else None
        return self.application.registry_repository_name(app_env)

    def docker_pull_credentials(self, secret):
        return generate_docker_credentials(
            secret=secret,
            resource_type="repository",
            resource_name=self.repository_name,
            resource_actions=["pull"],
        )

    def image_pull_secrets(self, secret, registry_urls=None):
        return generate_kubernetes_imagepullsecrets(
            secret=secret,
            registry_urls=registry_urls,
            resource_type="repository",
            resource_name=self.repository_name,
            resource_actions=["pull"],
        )

    @property
    def commit_sha(self):
        if self.release_metadata is None or self.release_metadata.get("sha") is None:
            return self.image_object.commit_sha
        return self.release_metadata.get("sha")

    @property
    def release_build_context_configmap(self):
        process_commands = "\n".join(
            [
                (
                    f"COPY envconsul-{process_name}.hcl "
                    "/etc/cabotage/envconsul-{process_name}.hcl"
                )
                for process_name in self.envconsul_configurations
            ]
        )
        dockerfile = RELEASE_DOCKERFILE_TEMPLATE.format(
            registry=current_app.config["REGISTRY_BUILD"],
            image=self.image_object,
            process_commands=process_commands,
        )
        if self.dockerfile:
            dockerfile = self.dockerfile
        return configmap_context_for_release(self, dockerfile)


@listens_for(Release, "before_insert")
def release_before_insert_listener(mapper, connection, target):
    filters = {
        "application_id": target.application_id,
        "application_environment_id": target.application_environment_id,
    }
    most_recent_release = (
        mapper.class_.query.filter_by(**filters)
        .order_by(mapper.class_.version.desc())
        .first()
    )
    if most_recent_release is None:
        target.version = 1
    else:
        target.version = most_recent_release.version + 1


class Configuration(db.Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "project_app_configurations"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    application_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("project_applications.id"),
        nullable=False,
    )
    application_environment_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("application_environments.id"),
        nullable=True,
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
    build_key_slug = db.Column(
        db.Text(),
        nullable=True,
    )
    version_id = db.Column(db.Integer, nullable=False)
    deleted = db.Column(db.Boolean, nullable=False, default=False)
    secret = db.Column(db.Boolean, nullable=False, default=False)
    buildtime = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (
        db.UniqueConstraint(
            application_id, application_environment_id, name,
            name="uq_project_app_configurations_app_env_name",
        ),
        db.Index(
            "uq_project_app_configurations_app_name_no_env",
            application_id, name,
            unique=True,
            postgresql_where=text("application_environment_id IS NULL"),
        ),
    )

    __mapper_args__ = {"version_id_col": version_id}

    @property
    def asdict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "version_id": self.version_id,
            "secret": self.secret,
        }

    @property
    def envconsul_statement(self):
        directive = "secret" if self.secret else "prefix"
        path = self.key_slug.split(":", 1)[1]
        return f"{directive} {{\n" "  no_prefix = true\n" f'  path = "{path}"\n' "}"

    def read_value(self, reader):
        if self.secret:
            if self.buildtime:
                payload = reader.read(
                    self.build_key_slug.split(":", 1)[1], build=True, secret=True
                )
                return payload["data"][self.name]
            return "**secret**"
        return self.value


class Hook(db.Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "hooks"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    commit_sha = db.Column(
        db.String(256),
        index=True,
        nullable=True,
    )
    headers = db.Column(
        postgresql.JSONB(),
        nullable=False,
    )
    payload = db.Column(
        postgresql.JSONB(),
        nullable=False,
    )
    processed = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )
    deployed = db.Column(
        db.Boolean,
        nullable=True,
        default=None,
    )
    version_id = db.Column(db.Integer, nullable=False)

    __mapper_args__ = {"version_id_col": version_id}


class Image(db.Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "project_app_images"

    id = db.Column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        nullable=False,
        primary_key=True,
    )
    application_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("project_applications.id"),
        nullable=False,
    )
    application_environment_id = db.Column(
        postgresql.UUID(as_uuid=True),
        db.ForeignKey("application_environments.id"),
        nullable=True,
    )

    _repository_name = db.Column(
        "repository_name",
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
    built = db.Column(db.Boolean, nullable=False, default=False)
    error = db.Column(db.Boolean, nullable=False, default=False)
    error_detail = db.Column(
        db.String(2048),
        nullable=True,
    )
    deleted = db.Column(db.Boolean, nullable=False, default=False)
    build_slug = db.Column(
        db.String(1024),
        nullable=True,
    )
    build_ref = db.Column(
        db.String(1024),
        nullable=True,
    )
    dockerfile = db.Column(
        db.Text(),
        nullable=True,
    )
    procfile = db.Column(
        db.Text(),
        nullable=True,
    )
    processes = db.Column(
        postgresql.JSONB(),
        nullable=True,
    )
    image_metadata = db.Column(
        postgresql.JSONB(),
        nullable=True,
    )
    image_build_log = db.Column(
        db.Text(),
        nullable=True,
    )
    build_job_id = db.Column(
        db.String(64),
        nullable=True,
    )

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (
        CheckConstraint(
            "NOT(build_ref IS NULL AND build_slug IS NULL)",
            name="image_has_build_target",
        ),
    )

    @property
    def repository_name(self):
        app_env = self.application_environment if self.application_environment_id else None
        return self.application.registry_repository_name(app_env)

    @property
    def asdict(self):
        return {
            "id": str(self.id),
            "repository": self.repository_name,
            "tag": str(self.version),
            "processes": self.processes,
        }

    def docker_pull_credentials(self, secret):
        return generate_docker_credentials(
            secret=secret,
            resource_type="repository",
            resource_name=self.repository_name,
            resource_actions=["pull"],
        )

    @staticmethod
    def _build_repository_name(org_k8s, project_k8s, app_k8s, env_k8s=None):
        if env_k8s is not None:
            return f"cabotage/{org_k8s}/{env_k8s}/{project_k8s}/{app_k8s}"
        return f"cabotage/{org_k8s}/{project_k8s}/{app_k8s}"

    def buildargs(self, reader):
        if self.application_environment_id is not None:
            configs = self.application_environment.configurations
        else:
            configs = self.application.configurations
        return {
            c.name: c.read_value(reader)
            for c in configs
            if c.buildtime
        }

    @property
    def commit_sha(self):
        if self.image_metadata is None or self.image_metadata.get("sha") is None:
            return "null"
        return self.image_metadata.get("sha")


@listens_for(Image, "before_insert")
def image_before_insert_listener(mapper, connection, target):
    filters = {
        "application_id": target.application_id,
        "application_environment_id": target.application_environment_id,
    }
    most_recent_image = (
        mapper.class_.query.filter_by(**filters)
        .order_by(mapper.class_.version.desc())
        .first()
    )
    if most_recent_image is None:
        target.version = 1
    else:
        target.version = most_recent_image.version + 1
