from __future__ import annotations

import datetime
import json
import uuid
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from cabotage.server.models.auth import Organization
    from cabotage.server.models.resources import Resource

from flask import current_app
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.event import listens_for
from sqlalchemy.orm import DynamicMapped, Mapped, mapped_column, relationship, backref
from sqlalchemy_continuum import make_versioned
from sqlalchemy_continuum.plugins import FlaskPlugin
from sqlalchemy_utils.models import Timestamp

from cabotage.server import db, Model

from cabotage.server.models.plugins import ActivityPlugin
from cabotage.server.models.utils import (
    generate_k8s_identifier,
    readable_k8s_hostname,
    safe_k8s_name,
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
flask_plugin = FlaskPlugin()
make_versioned(plugins=[activity_plugin, flask_plugin])

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


class Project(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "projects"

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
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("organizations.id"),
    )
    name: Mapped[str] = mapped_column(Text())
    slug: Mapped[str] = mapped_column(postgresql.CITEXT())
    k8s_identifier: Mapped[str] = mapped_column(String(64))
    environments_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    branch_deploys_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    branch_deploy_base_environment_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_environments.id"),
    )
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, index=True)

    organization: Mapped[Organization] = relationship(
        "Organization", back_populates="projects"
    )

    branch_deploy_base_environment: Mapped[Environment | None] = relationship(
        "Environment", foreign_keys=[branch_deploy_base_environment_id]
    )
    project_applications: Mapped[list[Application]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
    )
    project_environments: Mapped[list[Environment]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="Environment.sort_order",
        foreign_keys="Environment.project_id",
    )

    @property
    def active_applications(self):
        return [a for a in self.project_applications if a.deleted_at is None]

    @property
    def active_environments(self):
        return [e for e in self.project_environments if e.deleted_at is None]

    __table_args__ = (
        UniqueConstraint(organization_id, slug),
        UniqueConstraint(
            organization_id, k8s_identifier, name="uq_projects_org_k8s_identifier"
        ),
    )


class Environment(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "project_environments"

    def __init__(self, *args, **kwargs):
        if "slug" not in kwargs and "name" in kwargs:
            kwargs["slug"] = slugify(kwargs["name"])
        if "k8s_identifier" not in kwargs and "slug" in kwargs:
            kwargs["k8s_identifier"] = generate_k8s_identifier(kwargs["slug"])
        super().__init__(*args, **kwargs)

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("projects.id"),
        index=True,
    )
    name: Mapped[str] = mapped_column(Text())
    slug: Mapped[str] = mapped_column(postgresql.CITEXT())
    k8s_identifier: Mapped[str] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    ephemeral: Mapped[bool] = mapped_column(Boolean, default=False)
    ttl_hours: Mapped[int | None] = mapped_column(Integer)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    uses_environment_namespace: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, index=True)
    forked_from_environment_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_environments.id"),
    )
    version_id: Mapped[int] = mapped_column(Integer)

    project: Mapped[Project] = relationship(
        back_populates="project_environments", foreign_keys=[project_id]
    )
    forked_from_environment: Mapped[Environment | None] = relationship(
        "Environment",
        remote_side="Environment.id",
        foreign_keys=[forked_from_environment_id],
    )
    application_environments: Mapped[list[ApplicationEnvironment]] = relationship(
        back_populates="environment",
        cascade="all, delete-orphan",
    )
    environment_configurations: Mapped[list[EnvironmentConfiguration]] = relationship(
        back_populates="environment",
        cascade="all, delete-orphan",
        order_by="EnvironmentConfiguration.name",
    )
    resources: Mapped[list["Resource"]] = relationship(
        back_populates="environment",
        cascade="all, delete-orphan",
    )

    @property
    def active_application_environments(self):
        return [ae for ae in self.application_environments if ae.deleted_at is None]

    @property
    def active_environment_configurations(self):
        return [ec for ec in self.environment_configurations if not ec.deleted]

    @property
    def k8s_namespace(self):
        """The K8s namespace where resources for this environment live.

        Uses the combined org+env namespace when environment-scoped
        namespacing is explicitly enabled for this environment,
        otherwise falls back to the org namespace.
        """
        org_k8s = self.project.organization.k8s_identifier
        if self.uses_environment_namespace:
            return safe_k8s_name(org_k8s, self.k8s_identifier)
        return org_k8s

    @property
    def active_resources(self):
        return [r for r in self.resources if r.deleted_at is None]

    @property
    def active_postgres_resources(self):
        return [r for r in self.active_resources if r.type == "postgres"]

    @property
    def active_redis_resources(self):
        return [r for r in self.active_resources if r.type == "redis"]

    __table_args__ = (
        UniqueConstraint(project_id, slug),
        UniqueConstraint(
            project_id,
            k8s_identifier,
            name="uq_project_environments_project_k8s_identifier",
        ),
    )

    __mapper_args__ = {"version_id_col": version_id}


class ApplicationEnvironment(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "application_environments"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_applications.id"),
        index=True,
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_environments.id"),
        index=True,
    )
    process_counts: Mapped[Any | None] = mapped_column(
        postgresql.JSONB(), server_default=text("json_object('{}')")
    )
    process_pod_classes: Mapped[Any | None] = mapped_column(
        postgresql.JSONB(), server_default=text("json_object('{}')")
    )
    deployment_timeout: Mapped[int | None] = mapped_column(Integer)
    health_check_path: Mapped[str | None] = mapped_column(String(64))
    health_check_host: Mapped[str | None] = mapped_column(String(256))
    auto_deploy_branch: Mapped[str | None] = mapped_column(Text())
    auto_deploy_wait_for_ci: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
    )
    github_environment_name: Mapped[str | None] = mapped_column(Text())
    k8s_identifier: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, index=True)
    version_id: Mapped[int] = mapped_column(Integer)

    application: Mapped[Application] = relationship(
        back_populates="application_environments"
    )
    environment: Mapped[Environment] = relationship(
        back_populates="application_environments"
    )
    configurations: Mapped[list[Configuration]] = relationship(
        back_populates="application_environment",
        foreign_keys="Configuration.application_environment_id",
        order_by="Configuration.name",
    )
    environment_config_subscriptions: Mapped[list[EnvironmentConfigSubscription]] = (
        relationship(
            back_populates="application_environment",
            cascade="all, delete-orphan",
        )
    )
    images: DynamicMapped[Image] = relationship(
        back_populates="application_environment",
        foreign_keys="Image.application_environment_id",
        lazy="dynamic",
    )
    releases: DynamicMapped[Release] = relationship(
        back_populates="application_environment",
        foreign_keys="Release.application_environment_id",
        lazy="dynamic",
    )
    deployments: DynamicMapped[Deployment] = relationship(
        back_populates="application_environment",
        foreign_keys="Deployment.application_environment_id",
        lazy="dynamic",
    )
    ingresses: Mapped[list[Ingress]] = relationship(
        back_populates="application_environment",
        foreign_keys="Ingress.application_environment_id",
        cascade="all, delete-orphan",
    )
    alerts: Mapped[list[Alert]] = relationship(back_populates="application_environment")

    __table_args__ = (
        Index(
            "uq_app_env_active",
            application_id,
            environment_id,
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    __mapper_args__ = {"version_id_col": version_id}

    @property
    def env_slug_for_paths(self):
        """Return the environment slug for path-building if this is a real
        (non-legacy) enrollment, or None for legacy (k8s_identifier is NULL)."""
        if self.k8s_identifier is not None:
            return self.environment.slug
        return None

    @property
    def latest_image(self):
        return self.images.order_by(Image.version.desc()).first()

    @property
    def latest_image_built(self):
        return self.images.filter_by(built=True).order_by(Image.version.desc()).first()

    @property
    def latest_release(self):
        return self.releases.order_by(Release.version.desc()).first()

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
        return self.deployments.order_by(Deployment.created.desc()).first()

    @property
    def latest_deployment_completed(self):
        return (
            self.deployments.filter_by(complete=True)
            .order_by(Deployment.created.desc())
            .first()
        )

    @property
    def latest_deployment_running(self):
        return (
            self.deployments.filter_by(complete=False, error=False)
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
        if not self.application.project.environments_enabled:
            if self.application.github_environment_name is not None:
                return self.application.github_environment_name
        return f"{self.application.project.organization.slug}/{self.application.project.slug}/{self.environment.slug}/{self.application.slug}"

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


class Application(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "project_applications"

    def __init__(self, *args, **kwargs):
        if "slug" in kwargs and "k8s_identifier" not in kwargs:
            kwargs["k8s_identifier"] = generate_k8s_identifier(kwargs["slug"])
        super().__init__(*args, **kwargs)

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("projects.id"),
    )
    name: Mapped[str] = mapped_column(Text())
    slug: Mapped[str] = mapped_column(postgresql.CITEXT())
    k8s_identifier: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(platform_version, default="wind")
    process_counts: Mapped[Any | None] = mapped_column(
        postgresql.JSONB(), server_default=text("json_object('{}')")
    )
    process_pod_classes: Mapped[Any | None] = mapped_column(
        postgresql.JSONB(), server_default=text("json_object('{}')")
    )
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, index=True)

    project: Mapped[Project] = relationship(back_populates="project_applications")
    images: DynamicMapped[Image] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    configurations: Mapped[list[Configuration]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="Configuration.name",
    )
    releases: DynamicMapped[Release] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    deployments: DynamicMapped[Deployment] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    application_environments: Mapped[list[ApplicationEnvironment]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
    )
    alerts: Mapped[list[Alert]] = relationship(back_populates="application")

    @property
    def active_application_environments(self):
        return [ae for ae in self.application_environments if ae.deleted_at is None]

    version_id: Mapped[int] = mapped_column(Integer)

    github_app_installation_id: Mapped[int | None] = mapped_column(Integer)
    github_repository: Mapped[str | None] = mapped_column(Text())
    github_repository_is_private: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )
    github_environment_name: Mapped[str | None] = mapped_column(Text())

    subdirectory: Mapped[str | None] = mapped_column(Text())

    dockerfile_path: Mapped[str | None] = mapped_column(Text())
    branch_deploy_watch_paths: Mapped[Any | None] = mapped_column(
        postgresql.JSONB(),
    )

    auto_deploy_branch: Mapped[str | None] = mapped_column(Text())
    deployment_timeout: Mapped[int | None] = mapped_column(
        Integer,
        server_default="180",
    )

    health_check_path: Mapped[str] = mapped_column(
        String(64),
        server_default="/_health/",
    )
    health_check_host: Mapped[str | None] = mapped_column(
        String(256),
        server_default=None,
    )

    privileged: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    @property
    def default_app_env(self):
        """Return the implicit/default ApplicationEnvironment for this app."""
        active = self.active_application_environments
        return next(
            (ae for ae in active if ae.k8s_identifier is None),
            active[0] if active else None,
        )

    # Proxy properties that delegate to default_app_env so listing-page
    # templates (organization.html, projects.html, etc.) can use app.latest_*
    @property
    def latest_image_built(self):
        ae = self.default_app_env
        return ae.latest_image_built if ae else None

    @property
    def latest_image_error(self):
        ae = self.default_app_env
        return ae.latest_image_error if ae else None

    @property
    def latest_image_building(self):
        ae = self.default_app_env
        return ae.latest_image_building if ae else None

    @property
    def latest_release(self):
        ae = self.default_app_env
        return ae.latest_release if ae else None

    @property
    def latest_deployment_completed(self):
        ae = self.default_app_env
        return ae.latest_deployment_completed if ae else None

    @property
    def latest_deployment_running(self):
        ae = self.default_app_env
        return ae.latest_deployment_running if ae else None

    def ready_for_deployment_in_env(self, app_env):
        current = {}
        latest_deployed = app_env.latest_deployment_completed
        if latest_deployed:
            current = latest_deployed.release
        candidate = self.release_candidate_for_env(app_env)
        configuration_diff = DictDiffer(
            candidate.get("configuration") or {},
            current.get("configuration") or {},
            ignored_keys=["id"],
        )
        image_diff = DictDiffer(
            candidate.get("image") or {},
            current.get("image") or {},
            ignored_keys=["id", "commit_sha"],
        )
        ingress_diff = DictDiffer(
            candidate.get("ingresses") or {},
            current.get("ingresses") or {},
            ignored_keys=["id"],
        )
        return image_diff, configuration_diff, ingress_diff

    @staticmethod
    def _resolved_configuration(app_env):
        """Merge environment-level configs (base) with app-level configs (override)."""
        config = {}
        for sub in app_env.environment_config_subscriptions:
            ec = sub.environment_configuration
            if not ec.deleted:
                config[ec.name] = ec.asdict
        for c in app_env.configurations:
            if not c.deleted:
                config[c.name] = c.asdict
        return config

    def release_candidate_for_env(self, app_env):
        release = Release(
            application_id=self.id,
            application_environment_id=app_env.id,
            image=(
                app_env.latest_image_built.asdict if app_env.latest_image_built else {}
            ),
            configuration=self._resolved_configuration(app_env),
            ingresses={ing.name: ing.asdict for ing in app_env.ingresses},
            platform=self.platform,
        )
        return release.asdict

    def registry_repository_name(self, app_env):
        """Build the registry repo name using k8s identifiers."""
        org_k8s = self.project.organization.k8s_identifier
        project_k8s = self.project.k8s_identifier
        app_k8s = self.k8s_identifier
        env_k8s = (
            app_env.environment.k8s_identifier
            if app_env.environment.uses_environment_namespace
            else None
        )
        return Image._build_repository_name(org_k8s, project_k8s, app_k8s, env_k8s)

    def create_release(self, app_env):
        image_diff, configuration_diff, ingress_diff = self.ready_for_deployment_in_env(
            app_env
        )
        release = Release(
            application_id=self.id,
            application_environment_id=app_env.id,
            image=(
                app_env.latest_image_built.asdict if app_env.latest_image_built else {}
            ),
            _repository_name=self.registry_repository_name(app_env),
            configuration=self._resolved_configuration(app_env),
            image_changes=image_diff.asdict,
            configuration_changes=configuration_diff.asdict,
            ingresses={ing.name: ing.asdict for ing in app_env.ingresses},
            ingress_changes=ingress_diff.asdict,
            platform=self.platform,
            health_check_path=app_env.effective_health_check_path,
            health_check_host=app_env.effective_health_check_host,
        )
        return release

    __table_args__ = (
        UniqueConstraint(project_id, slug),
        UniqueConstraint(
            project_id,
            k8s_identifier,
            name="uq_project_applications_project_k8s_identifier",
        ),
        Index(
            "github_deployments_unique",
            github_app_installation_id,
            github_repository,
            github_environment_name,
            unique=True,
            postgresql_where=(github_environment_name is not None),
        ),
    )

    __mapper_args__ = {"version_id_col": version_id}


class Deployment(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "deployments"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_applications.id"),
        index=True,
    )
    application_environment_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("application_environments.id"),
        index=True,
    )
    release: Mapped[Any] = mapped_column(postgresql.JSONB())
    version_id: Mapped[int] = mapped_column(Integer)
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[bool] = mapped_column(Boolean, default=False)
    error_detail: Mapped[str | None] = mapped_column(String(2048))
    deploy_metadata: Mapped[Any | None] = mapped_column(postgresql.JSONB())
    deploy_log: Mapped[str | None] = mapped_column(Text())
    job_id: Mapped[str | None] = mapped_column(String(64))

    application: Mapped[Application] = relationship(back_populates="deployments")
    application_environment: Mapped[ApplicationEnvironment] = relationship(
        back_populates="deployments", foreign_keys=[application_environment_id]
    )

    __mapper_args__ = {"version_id_col": version_id}

    @property
    def release_object(self):
        return Release.query.filter_by(id=self.release.get("id", None)).first()

    @property
    def release_snapshot(self):
        if self.release:
            return ReleaseSnapshot(self.release)
        return None


class JobLog(Model, Timestamp):
    __tablename__ = "job_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_applications.id"),
        index=True,
    )
    application_environment_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("application_environments.id"),
        index=True,
    )
    process_name: Mapped[str] = mapped_column(String(64))
    job_name: Mapped[str] = mapped_column(String(253))
    namespace: Mapped[str] = mapped_column(String(253))
    schedule_timestamp: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    start_time: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    completion_time: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    succeeded: Mapped[bool] = mapped_column(Boolean)
    pods_active: Mapped[int] = mapped_column(Integer, default=0)
    pods_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    pods_failed: Mapped[int] = mapped_column(Integer, default=0)
    release_version: Mapped[int | None] = mapped_column(Integer)
    deployment_id: Mapped[str | None] = mapped_column(String(64))
    labels: Mapped[Any | None] = mapped_column(postgresql.JSONB())
    resources: Mapped[Any | None] = mapped_column(postgresql.JSONB())

    __table_args__ = (
        UniqueConstraint(
            "job_name", "namespace", name="uq_job_logs_job_name_namespace"
        ),
        Index(
            "ix_job_logs_app_env_process_completion",
            "application_id",
            "application_environment_id",
            "process_name",
            completion_time.desc(),
        ),
    )

    application: Mapped[Application] = relationship(
        backref=backref("job_logs", lazy="dynamic"),
    )
    application_environment: Mapped[ApplicationEnvironment] = relationship(
        backref=backref("job_logs", lazy="dynamic"),
    )


class Release(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "project_app_releases"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_applications.id"),
        index=True,
    )
    application_environment_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("application_environments.id"),
        index=True,
    )
    platform: Mapped[str] = mapped_column(platform_version, default="wind")
    image: Mapped[Any] = mapped_column(postgresql.JSONB())
    configuration: Mapped[Any] = mapped_column(postgresql.JSONB())
    image_changes: Mapped[Any] = mapped_column(postgresql.JSONB())
    configuration_changes: Mapped[Any] = mapped_column(postgresql.JSONB())
    ingresses: Mapped[Any] = mapped_column(
        postgresql.JSONB(),
        server_default=text("'{}'::jsonb"),
    )
    ingress_changes: Mapped[Any] = mapped_column(
        postgresql.JSONB(),
        server_default=text("'{}'::jsonb"),
    )
    version_id: Mapped[int] = mapped_column(Integer)

    _repository_name: Mapped[str] = mapped_column(
        "repository_name",
        String(256),
    )
    release_id: Mapped[str | None] = mapped_column(String(256))
    version: Mapped[int] = mapped_column(Integer)

    built: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[bool] = mapped_column(Boolean, default=False)
    error_detail: Mapped[str | None] = mapped_column(String(2048))
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    dockerfile: Mapped[str | None] = mapped_column(Text())
    release_metadata: Mapped[Any | None] = mapped_column(postgresql.JSONB())
    release_build_log: Mapped[str | None] = mapped_column(Text())
    build_job_id: Mapped[str | None] = mapped_column(String(64))
    health_check_path: Mapped[str] = mapped_column(
        String(64),
        server_default="/_health/",
    )
    health_check_host: Mapped[str | None] = mapped_column(
        String(256),
        server_default=None,
    )

    application: Mapped[Application] = relationship(back_populates="releases")
    application_environment: Mapped[ApplicationEnvironment] = relationship(
        back_populates="releases", foreign_keys=[application_environment_id]
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
                f"<code>Image {self.image['repository']}:{self.image['tag']} "
                "no longer exists!</code>"
            )
        for configuration, configuration_serialized in self.configuration.items():
            configuration_object = Configuration.query.filter_by(
                id=configuration_serialized["id"]
            ).first()
            if configuration_object is None:
                configuration_object = EnvironmentConfiguration.query.filter_by(
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
            "ingresses": self.ingresses,
        }

    @property
    def configuration_objects(self):
        result = {}
        for k, v in self.configuration.items():
            obj = Configuration.query.filter_by(id=v["id"]).first()
            if obj is None:
                obj = EnvironmentConfiguration.query.filter_by(id=v["id"]).first()
            result[k] = obj
        return result

    @property
    def envconsul_configurations(self):
        from cabotage.utils.config_templates import (
            has_template_variables,
            resolve_shared_secret_refs,
            resolve_template_variables,
        )

        configurations = {}
        config_objects = [
            c for c in self.configuration_objects.values() if c is not None
        ]

        # Separate template configs from regular configs
        statements = []
        resolved_template_env = []
        for c in config_objects:
            if has_template_variables(c.value):
                # Check for whole-value shared secret ref: MY_VAR=${shared.SECRET}
                # These get a vault directive with key format renaming
                secret_refs = resolve_shared_secret_refs(
                    c.value, self.application_environment
                )
                if secret_refs:
                    for _orig_name, env_cfg in secret_refs:
                        if env_cfg.key_slug:
                            path = env_cfg.key_slug.split(":", 1)[1]
                            stmt = (
                                "secret {\n"
                                "  no_prefix = true\n"
                                f'  path = "{path}"\n'
                                "  key {\n"
                                f'    name   = "{env_cfg.name}"\n'
                                f'    format = "{c.name}"\n'
                                "  }\n"
                                "}"
                            )
                            statements.append(stmt)
                    continue
                resolved = resolve_template_variables(
                    c.value, self.application_environment
                )
                resolved_template_env.append(f"{c.name}={resolved}")
            else:
                stmt = c.envconsul_statement
                if stmt is not None:
                    statements.append(stmt)
        environment_statements = "\n".join(statements)

        exec_statement = 'exec {\n  command = "/bin/sh"\n'
        if not self.application.privileged:
            exec_statement += "  env = {\n"
            if resolved_template_env:
                exec_statement += f"    custom = {json.dumps(resolved_template_env)}\n"
            exec_statement += (
                '    denylist = ["CONSUL_*", "VAULT_*", "KUBERNETES_*"]\n  }\n'
            )
        exec_statement += "}"
        configurations["shell"] = "\n".join([exec_statement, environment_statements])
        for proc_name, proc in self.image_snapshot.processes.items():
            proc_env = [f"{key}={value}" for key, value in proc["env"]]
            proc_env.extend(resolved_template_env)
            custom_env = json.dumps(proc_env)
            exec_statement = f"exec {{\n  command = {json.dumps(proc['cmd'])}\n"
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
    def image_snapshot(self):
        if self.image:
            return ImageSnapshot(self.image)
        return None

    @property
    def configuration_snapshots(self):
        return {k: ConfigurationSnapshot(v) for k, v in self.configuration.items()}

    @property
    def processes(self):
        if not self.image_snapshot:
            return {}
        return {
            k: v
            for k, v in self.image_snapshot.processes.items()
            if not (
                k.startswith("release")
                or k.startswith("postdeploy")
                or k.startswith("job")
            )
        }

    @property
    def release_commands(self):
        if not self.image_snapshot:
            return {}
        return {
            k: v
            for k, v in self.image_snapshot.processes.items()
            if k.startswith("release")
        }

    @property
    def postdeploy_commands(self):
        if not self.image_snapshot:
            return {}
        return {
            k: v
            for k, v in self.image_snapshot.processes.items()
            if k.startswith("postdeploy")
        }

    @property
    def job_processes(self):
        if not self.image_snapshot:
            return {}
        return {
            k: v
            for k, v in self.image_snapshot.processes.items()
            if k.startswith("job")
        }

    @property
    def repository_name(self):
        return self._repository_name

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
        if self.release_metadata and self.release_metadata.get("sha"):
            return self.release_metadata.get("sha")
        return self.image_snapshot.commit_sha

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
            image=self.image_snapshot,
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


class Configuration(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "project_app_configurations"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_applications.id"),
        index=True,
    )
    application_environment_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("application_environments.id"),
        index=True,
    )

    name: Mapped[str] = mapped_column(postgresql.CITEXT())
    value: Mapped[str] = mapped_column(String(2048))
    key_slug: Mapped[str | None] = mapped_column(Text())
    build_key_slug: Mapped[str | None] = mapped_column(Text())
    version_id: Mapped[int] = mapped_column(Integer)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    secret: Mapped[bool] = mapped_column(Boolean, default=False)
    buildtime: Mapped[bool] = mapped_column(Boolean, default=False)

    application: Mapped[Application] = relationship(back_populates="configurations")
    application_environment: Mapped[ApplicationEnvironment] = relationship(
        back_populates="configurations", foreign_keys=[application_environment_id]
    )

    __table_args__ = (
        UniqueConstraint(
            application_id,
            application_environment_id,
            name,
            name="uq_project_app_configurations_app_env_name",
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
            "buildtime": self.buildtime,
        }

    @property
    def envconsul_statement(self):
        from cabotage.utils.config_templates import has_template_variables

        if has_template_variables(self.value):
            return None
        if not self.key_slug:
            return None
        directive = "secret" if self.secret else "prefix"
        path = self.key_slug.split(":", 1)[1]
        return f'{directive} {{\n  no_prefix = true\n  path = "{path}"\n}}'

    def read_value(self, reader):
        from cabotage.utils.config_templates import (
            has_template_variables,
            resolve_template_variables,
        )

        if self.secret:
            if self.buildtime and self.build_key_slug:
                payload = reader.read(
                    self.build_key_slug.split(":", 1)[1], build=True, secret=True
                )
                return payload["data"][self.name]
            return "**secret**"
        if has_template_variables(self.value):
            return resolve_template_variables(
                self.value, self.application_environment, reader=reader
            )
        return self.value


class ConfigurationSnapshot:
    """Read-only wrapper over serialized Configuration JSONB data."""

    def __init__(self, data):
        self.id = data["id"]
        self.name = data["name"]
        self.version_id = data["version_id"]
        self.secret = data["secret"]
        self.buildtime = data.get("buildtime", False)


class EnvironmentConfiguration(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "project_environment_configurations"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("projects.id"),
        index=True,
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_environments.id"),
        index=True,
    )
    name: Mapped[str] = mapped_column(postgresql.CITEXT())
    value: Mapped[str] = mapped_column(String(2048))
    key_slug: Mapped[str | None] = mapped_column(Text())
    build_key_slug: Mapped[str | None] = mapped_column(Text())
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("resources.id"),
        index=True,
    )
    version_id: Mapped[int] = mapped_column(Integer)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    secret: Mapped[bool] = mapped_column(Boolean, default=False)
    buildtime: Mapped[bool] = mapped_column(Boolean, default=False)

    environment: Mapped[Environment] = relationship(
        back_populates="environment_configurations"
    )
    subscriptions: Mapped[list[EnvironmentConfigSubscription]] = relationship(
        back_populates="environment_configuration",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            project_id,
            environment_id,
            name,
            name="uq_project_env_configurations_project_env_name",
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
            "buildtime": self.buildtime,
        }

    @property
    def envconsul_statement(self):
        from cabotage.utils.config_templates import has_template_variables

        if has_template_variables(self.value):
            return None
        if not self.key_slug:
            return None
        directive = "secret" if self.secret else "prefix"
        path = self.key_slug.split(":", 1)[1]
        return f'{directive} {{\n  no_prefix = true\n  path = "{path}"\n}}'

    def read_value(self, reader):
        from cabotage.utils.config_templates import has_template_variables

        if self.secret:
            if self.buildtime and self.build_key_slug:
                payload = reader.read(
                    self.build_key_slug.split(":", 1)[1], build=True, secret=True
                )
                return payload["data"][self.name]
            return "**secret**"
        if has_template_variables(self.value):
            return self.value
        return self.value


class EnvironmentConfigSubscription(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "environment_config_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    application_environment_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("application_environments.id"),
        index=True,
    )
    environment_configuration_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_environment_configurations.id"),
        index=True,
    )

    application_environment: Mapped[ApplicationEnvironment] = relationship(
        back_populates="environment_config_subscriptions"
    )
    environment_configuration: Mapped[EnvironmentConfiguration] = relationship(
        back_populates="subscriptions"
    )

    __table_args__ = (
        UniqueConstraint(
            application_environment_id,
            environment_configuration_id,
            name="uq_env_config_subscription_app_env_config",
        ),
    )


class Hook(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "hooks"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    commit_sha: Mapped[str | None] = mapped_column(
        String(256),
        index=True,
    )
    headers: Mapped[Any] = mapped_column(postgresql.JSONB())
    payload: Mapped[Any] = mapped_column(postgresql.JSONB())
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    deployed: Mapped[bool | None] = mapped_column(Boolean, default=None)
    version_id: Mapped[int] = mapped_column(Integer)

    __mapper_args__ = {"version_id_col": version_id}


class Image(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "project_app_images"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_applications.id"),
        index=True,
    )
    application_environment_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("application_environments.id"),
        index=True,
    )

    application: Mapped[Application] = relationship(back_populates="images")
    application_environment: Mapped[ApplicationEnvironment] = relationship(
        back_populates="images", foreign_keys=[application_environment_id]
    )

    _repository_name: Mapped[str] = mapped_column(
        "repository_name",
        String(256),
    )
    image_id: Mapped[str | None] = mapped_column(String(256))
    version: Mapped[int] = mapped_column(Integer)

    version_id: Mapped[int] = mapped_column(Integer)
    built: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[bool] = mapped_column(Boolean, default=False)
    error_detail: Mapped[str | None] = mapped_column(String(2048))
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    build_slug: Mapped[str | None] = mapped_column(String(1024))
    build_ref: Mapped[str | None] = mapped_column(String(1024))
    dockerfile: Mapped[str | None] = mapped_column(Text())
    procfile: Mapped[str | None] = mapped_column(Text())
    processes: Mapped[Any | None] = mapped_column(postgresql.JSONB())
    image_metadata: Mapped[Any | None] = mapped_column(postgresql.JSONB())
    image_build_log: Mapped[str | None] = mapped_column(Text())
    build_job_id: Mapped[str | None] = mapped_column(String(64))

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (
        CheckConstraint(
            "NOT(build_ref IS NULL AND build_slug IS NULL)",
            name="image_has_build_target",
        ),
    )

    @property
    def repository_name(self):
        return self._repository_name

    @property
    def asdict(self):
        return {
            "id": str(self.id),
            "repository": self.repository_name,
            "tag": str(self.version),
            "processes": self.processes,
            "commit_sha": self.commit_sha,
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
        args = {}
        # Subscribed env configs first (base), then app configs (override)
        for sub in self.application_environment.environment_config_subscriptions:
            ec = sub.environment_configuration
            if ec.buildtime and not ec.deleted:
                args[ec.name] = ec.read_value(reader)
        for c in self.application_environment.configurations:
            if c.buildtime:
                args[c.name] = c.read_value(reader)
        return args

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


class ImageSnapshot:
    """Read-only wrapper over serialized Image JSONB data."""

    def __init__(self, data):
        self.id = data["id"]
        self.repository = data["repository"]
        self.tag = data["tag"]
        self.processes = data.get("processes", {})
        self.commit_sha = data.get("commit_sha", "null")

    # Aliases matching Image model attribute names so this can be
    # used as a drop-in replacement (e.g. in RELEASE_DOCKERFILE_TEMPLATE).
    @property
    def repository_name(self):
        return self.repository

    @property
    def version(self):
        return self.tag


class Ingress(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "ingresses"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    application_environment_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("application_environments.id"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64), default="default")
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True)
    ingress_class_name: Mapped[str] = mapped_column(String(64), default="nginx")
    backend_protocol: Mapped[str] = mapped_column(String(16), default="HTTPS")
    proxy_connect_timeout: Mapped[str | None] = mapped_column(
        String(16), default="10s", server_default="10s"
    )
    proxy_read_timeout: Mapped[str | None] = mapped_column(
        String(16), default="10s", server_default="10s"
    )
    proxy_send_timeout: Mapped[str | None] = mapped_column(
        String(16), default="10s", server_default="10s"
    )
    proxy_body_size: Mapped[str | None] = mapped_column(
        String(16), default="10M", server_default="10M"
    )
    client_body_buffer_size: Mapped[str | None] = mapped_column(
        String(16), default="1M", server_default="1M"
    )
    proxy_request_buffering: Mapped[str | None] = mapped_column(
        String(16), default="on", server_default="on"
    )
    session_affinity: Mapped[bool] = mapped_column(Boolean(), default=False)
    use_regex: Mapped[bool] = mapped_column(Boolean(), default=False)
    allow_annotations: Mapped[bool] = mapped_column(Boolean(), default=False)
    extra_annotations: Mapped[Any] = mapped_column(
        postgresql.JSONB(), server_default=text("'{}'::jsonb")
    )
    cluster_issuer: Mapped[str] = mapped_column(String(64), default="letsencrypt")
    force_ssl_redirect: Mapped[bool] = mapped_column(Boolean(), default=True)
    service_upstream: Mapped[bool] = mapped_column(Boolean(), default=True)
    tailscale_hostname: Mapped[str | None] = mapped_column(String(253))
    tailscale_funnel: Mapped[bool] = mapped_column(Boolean(), default=False)
    tailscale_tags: Mapped[str | None] = mapped_column(String(512))
    version_id: Mapped[int] = mapped_column(Integer)

    application_environment: Mapped[ApplicationEnvironment] = relationship(
        back_populates="ingresses", foreign_keys=[application_environment_id]
    )
    hosts: Mapped[list[IngressHost]] = relationship(
        backref="ingress",
        cascade="all, delete-orphan",
        order_by="IngressHost.hostname",
    )
    paths: Mapped[list[IngressPath]] = relationship(
        backref="ingress",
        cascade="all, delete-orphan",
        order_by="IngressPath.path",
    )

    __table_args__ = (UniqueConstraint(application_environment_id, name),)

    __mapper_args__ = {"version_id_col": version_id}

    @property
    def asdict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "enabled": self.enabled,
            "ingress_class_name": self.ingress_class_name,
            "backend_protocol": self.backend_protocol,
            "proxy_connect_timeout": self.proxy_connect_timeout,
            "proxy_read_timeout": self.proxy_read_timeout,
            "proxy_send_timeout": self.proxy_send_timeout,
            "proxy_body_size": self.proxy_body_size,
            "client_body_buffer_size": self.client_body_buffer_size,
            "proxy_request_buffering": self.proxy_request_buffering,
            "session_affinity": self.session_affinity,
            "use_regex": self.use_regex,
            "allow_annotations": self.allow_annotations,
            "extra_annotations": self.extra_annotations,
            "cluster_issuer": self.cluster_issuer,
            "force_ssl_redirect": self.force_ssl_redirect,
            "service_upstream": self.service_upstream,
            "tailscale_hostname": self.tailscale_hostname,
            "tailscale_funnel": self.tailscale_funnel,
            "tailscale_tags": self.tailscale_tags,
            "hosts": sorted(
                [h.asdict for h in self.hosts], key=lambda h: h["hostname"]
            ),
            "paths": sorted([p.asdict for p in self.paths], key=lambda p: p["path"]),
        }

    def __repr__(self):
        return f"<Ingress {self.id} {self.name}>"


class IngressHost(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "ingress_hosts"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    ingress_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("ingresses.id"),
        index=True,
    )
    hostname: Mapped[str] = mapped_column(String(253))
    tls_enabled: Mapped[bool] = mapped_column(Boolean(), default=True)
    is_auto_generated: Mapped[bool] = mapped_column(Boolean(), default=False)
    version_id: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        Index(
            "ix_ingress_hosts_hostname_unique",
            hostname,
            unique=True,
            postgresql_where=text("NOT is_auto_generated"),
        ),
    )

    __mapper_args__ = {"version_id_col": version_id}

    @property
    def asdict(self):
        return {
            "id": str(self.id),
            "hostname": self.hostname,
            "tls_enabled": self.tls_enabled,
            "is_auto_generated": self.is_auto_generated,
        }

    def __repr__(self):
        return f"<IngressHost {self.id} {self.hostname}>"


class IngressPath(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "ingress_paths"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    ingress_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("ingresses.id"),
        index=True,
    )
    path: Mapped[str] = mapped_column(String(256), default="/")
    path_type: Mapped[str] = mapped_column(String(32), default="Prefix")
    target_process_name: Mapped[str] = mapped_column(String(64))
    version_id: Mapped[int] = mapped_column(Integer)

    @property
    def asdict(self):
        return {
            "id": str(self.id),
            "path": self.path,
            "path_type": self.path_type,
            "target_process_name": self.target_process_name,
        }

    __table_args__ = (UniqueConstraint(ingress_id, path),)

    __mapper_args__ = {"version_id_col": version_id}


class IngressHostSnapshot:
    """Read-only wrapper over serialized IngressHost JSONB data."""

    def __init__(self, data):
        self.hostname = data["hostname"]
        self.tls_enabled = data["tls_enabled"]
        self.is_auto_generated = data["is_auto_generated"]


class IngressPathSnapshot:
    """Read-only wrapper over serialized IngressPath JSONB data."""

    def __init__(self, data):
        self.path = data["path"]
        self.path_type = data["path_type"]
        self.target_process_name = data["target_process_name"]


class IngressSnapshot:
    """Read-only wrapper over serialized Ingress JSONB data."""

    def __init__(self, data):
        self.name = data["name"]
        self.enabled = data["enabled"]
        self.ingress_class_name = data["ingress_class_name"]
        self.backend_protocol = data["backend_protocol"]
        self.proxy_connect_timeout = data.get("proxy_connect_timeout")
        self.proxy_read_timeout = data.get("proxy_read_timeout")
        self.proxy_send_timeout = data.get("proxy_send_timeout")
        self.proxy_body_size = data.get("proxy_body_size")
        self.client_body_buffer_size = data.get("client_body_buffer_size")
        self.proxy_request_buffering = data.get("proxy_request_buffering")
        self.session_affinity = data["session_affinity"]
        self.use_regex = data["use_regex"]
        self.allow_annotations = data["allow_annotations"]
        self.extra_annotations = data.get("extra_annotations", {})
        self.cluster_issuer = data["cluster_issuer"]
        self.force_ssl_redirect = data["force_ssl_redirect"]
        self.service_upstream = data["service_upstream"]
        self.tailscale_hostname = data.get("tailscale_hostname")
        self.tailscale_funnel = data.get("tailscale_funnel", False)
        self.tailscale_tags = data.get("tailscale_tags")
        self.hosts = [IngressHostSnapshot(h) for h in data.get("hosts", [])]
        self.paths = [IngressPathSnapshot(p) for p in data.get("paths", [])]


class ReleaseSnapshot:
    """Read-only wrapper over serialized Release JSONB data."""

    def __init__(self, data):
        self.id = data["id"]
        self.application_id = data["application_id"]
        self.platform = data["platform"]
        self.image_snapshot = (
            ImageSnapshot(data["image"]) if data.get("image") else None
        )
        self.configuration_snapshots = {
            k: ConfigurationSnapshot(v)
            for k, v in data.get("configuration", {}).items()
        }
        self.ingress_snapshots = [
            IngressSnapshot(v) for v in data.get("ingresses", {}).values()
        ]

    @property
    def processes(self):
        if not self.image_snapshot:
            return {}
        return {
            k: v
            for k, v in self.image_snapshot.processes.items()
            if not (
                k.startswith("release")
                or k.startswith("postdeploy")
                or k.startswith("job")
            )
        }

    @property
    def release_commands(self):
        if not self.image_snapshot:
            return {}
        return {
            k: v
            for k, v in self.image_snapshot.processes.items()
            if k.startswith("release")
        }

    @property
    def postdeploy_commands(self):
        if not self.image_snapshot:
            return {}
        return {
            k: v
            for k, v in self.image_snapshot.processes.items()
            if k.startswith("postdeploy")
        }

    @property
    def job_processes(self):
        if not self.image_snapshot:
            return {}
        return {
            k: v
            for k, v in self.image_snapshot.processes.items()
            if k.startswith("job")
        }

    @property
    def commit_sha(self):
        if self.image_snapshot:
            return self.image_snapshot.commit_sha
        return "null"


def _ingress_hostname_pairs(app_env):
    """Build (slug, k8s_identifier) pairs for ingress hostname generation.

    Skips the environment pair when environments are not enabled so that
    non-environment apps don't get "default" baked into their hostnames.
    """
    app = app_env.application
    org = app.project.organization
    project = app.project
    pairs = [(org.slug, org.k8s_identifier)]
    if project.environments_enabled:
        env = app_env.environment
        pairs.append((env.slug, env.k8s_identifier))
    pairs.extend(
        [
            (project.slug, project.k8s_identifier),
            (app.slug, app.k8s_identifier),
        ]
    )
    return pairs


def create_default_ingresses(app_env, process_names=None):
    """Create default Ingress records for web processes on an app_env.

    Called at app_env creation time so ingresses exist before the first release.
    If process_names is None, uses ["web"] as default.
    """
    ingress_domain = current_app.config.get("INGRESS_DOMAIN")
    if not ingress_domain:
        return

    if app_env.ingresses:
        return

    if process_names is None:
        process_names = ["web"]

    hostname_pairs = _ingress_hostname_pairs(app_env)

    for process_name in process_names:
        if not process_name.startswith("web"):
            continue
        ingress_obj = Ingress(
            application_environment_id=app_env.id,
            name=process_name,
        )
        db.session.add(ingress_obj)
        db.session.flush()
        auto_hostname = (
            f"{readable_k8s_hostname(*hostname_pairs)}-{process_name}.{ingress_domain}"
        )
        db.session.add(
            IngressHost(
                ingress_id=ingress_obj.id,
                hostname=auto_hostname,
                tls_enabled=True,
                is_auto_generated=True,
            )
        )
        db.session.add(
            IngressPath(
                ingress_id=ingress_obj.id,
                path="/",
                path_type="Prefix",
                target_process_name=process_name,
            )
        )
    db.session.flush()


class Alert(Model, Timestamp):
    __versioned__: dict = {}
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    fingerprint: Mapped[str] = mapped_column(String(256), index=True)
    status: Mapped[str] = mapped_column(String(32))
    alertname: Mapped[str] = mapped_column(String(256), index=True)
    labels: Mapped[Any] = mapped_column(postgresql.JSONB())
    annotations: Mapped[Any] = mapped_column(
        postgresql.JSONB(), server_default=text("'{}'::jsonb")
    )
    starts_at: Mapped[datetime.datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    generator_url: Mapped[str | None] = mapped_column(Text())
    group_key: Mapped[str | None] = mapped_column(Text())
    receiver: Mapped[str | None] = mapped_column(String(256))
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("project_applications.id"),
        index=True,
    )
    application_environment_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("application_environments.id"),
        index=True,
    )
    last_notified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    version_id: Mapped[int] = mapped_column(Integer)

    application: Mapped[Application | None] = relationship(
        back_populates="alerts",
    )
    application_environment: Mapped[ApplicationEnvironment | None] = relationship(
        back_populates="alerts",
    )

    __table_args__ = (
        Index(
            "ix_alerts_fingerprint_status",
            "fingerprint",
            "status",
        ),
        Index(
            "ix_alerts_fingerprint_starts_at",
            "fingerprint",
            "starts_at",
            unique=True,
        ),
    )

    __mapper_args__ = {"version_id_col": version_id}
