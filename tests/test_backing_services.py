"""Tests for backing service resources (Postgres and Redis)."""

import datetime
import time
import uuid
from unittest.mock import MagicMock

import pytest
from flask_security import hash_password
from kubernetes.client.rest import ApiException

from cabotage.server import db
from cabotage.server.models.auth import User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    EnvironmentConfiguration,
    Project,
)
from cabotage.server.models.resources import (
    PostgresResource,
    RedisResource,
    Resource,
    compute_postgres_parameters,
    postgres_size_classes,
)
from cabotage.server.models.utils import safe_k8s_name
from cabotage.server.wsgi import app as _app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REQUIRE_MFA"] = False
    with _app.app_context():
        yield _app
    # Restore defaults so we don't pollute other test files
    _app.config["REQUIRE_MFA"] = True


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_user(app):
    u = User(
        username=f"testadmin-{uuid.uuid4().hex[:8]}",
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.execute(
        db.text("DELETE FROM activity WHERE object_id = :uid"), {"uid": u.id}
    )
    db.session.execute(
        db.text("UPDATE transaction SET user_id = NULL WHERE user_id = :uid"),
        {"uid": u.id},
    )
    db.session.delete(u)
    db.session.commit()


@pytest.fixture
def org(admin_user):
    o = Organization(name="Test Org", slug=f"testorg-{uuid.uuid4().hex[:8]}")
    db.session.add(o)
    db.session.flush()
    membership = OrganizationMember(
        organization_id=o.id, user_id=admin_user.id, admin=True
    )
    db.session.add(membership)
    db.session.commit()
    yield o
    db.session.rollback()
    # Clean up in correct order for FK constraints
    for p in Project.query.filter_by(organization_id=o.id).all():
        for e in Environment.query.filter_by(project_id=p.id).all():
            EnvironmentConfiguration.query.filter_by(environment_id=e.id).delete()
            # Delete subtypes before base (FK constraint)
            for r in Resource.query.filter_by(environment_id=e.id).all():
                if r.type == "postgres":
                    db.session.execute(
                        db.text("DELETE FROM resources_postgres WHERE id = :rid"),
                        {"rid": r.id},
                    )
                elif r.type == "redis":
                    db.session.execute(
                        db.text("DELETE FROM resources_redis WHERE id = :rid"),
                        {"rid": r.id},
                    )
            Resource.query.filter_by(environment_id=e.id).delete()
            db.session.flush()
    OrganizationMember.query.filter_by(organization_id=o.id).delete()
    for p in Project.query.filter_by(organization_id=o.id).all():
        for e in Environment.query.filter_by(project_id=p.id).all():
            db.session.delete(e)
        db.session.delete(p)
    db.session.flush()
    db.session.delete(o)
    db.session.commit()


@pytest.fixture
def project(org):
    p = Project(name="My Project", organization_id=org.id)
    db.session.add(p)
    db.session.flush()
    return p


@pytest.fixture
def environment(project):
    e = Environment(name="staging", project_id=project.id, ephemeral=False)
    db.session.add(e)
    db.session.flush()
    return e


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"


def _configure_s3_tenant_postgres_backups(app):
    app.config.update(
        {
            "TENANT_POSTGRES_BACKUPS_ENABLED": True,
            "TENANT_POSTGRES_BACKUP_PROVIDER": "s3",
            "TENANT_POSTGRES_BACKUP_BUCKET": "tenant-postgres-backups",
            "TENANT_POSTGRES_BACKUP_IRSA_ROLE_ARN": (
                "arn:aws:iam::123456789012:role/tenant-postgres-backups"
            ),
            "TENANT_POSTGRES_BACKUP_PATH_PREFIX": "tenants",
            "TENANT_POSTGRES_BACKUP_PLUGIN_NAME": "barman-cloud.cloudnative-pg.io",
            "TENANT_POSTGRES_BACKUP_RETENTION_POLICY": "30d",
            "TENANT_POSTGRES_BACKUP_SCHEDULE": "0 0 0 * * *",
            "TENANT_POSTGRES_BACKUP_SERVICE_ACCOUNT_NAME": "cnpg-backups",
            "TENANT_POSTGRES_BACKUP_RUSTFS_ENDPOINT": None,
            "TENANT_POSTGRES_BACKUP_RUSTFS_CA_SECRET_NAME": "operators-ca-crt",
            "TENANT_POSTGRES_BACKUP_RUSTFS_SECRET_NAME": "cnpg-backups-objectstore",
            "TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAME": None,
            "TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAMESPACE": None,
        }
    )


def _configure_rustfs_tenant_postgres_backups(app):
    app.config.update(
        {
            "TENANT_POSTGRES_BACKUPS_ENABLED": True,
            "TENANT_POSTGRES_BACKUP_PROVIDER": "rustfs",
            "TENANT_POSTGRES_BACKUP_BUCKET": "cabotage-postgres-backups",
            "TENANT_POSTGRES_BACKUP_IRSA_ROLE_ARN": None,
            "TENANT_POSTGRES_BACKUP_PATH_PREFIX": "tenants",
            "TENANT_POSTGRES_BACKUP_PLUGIN_NAME": "barman-cloud.cloudnative-pg.io",
            "TENANT_POSTGRES_BACKUP_RETENTION_POLICY": "30d",
            "TENANT_POSTGRES_BACKUP_SCHEDULE": "0 0 0 * * *",
            "TENANT_POSTGRES_BACKUP_SERVICE_ACCOUNT_NAME": "cnpg-backups",
            "TENANT_POSTGRES_BACKUP_RUSTFS_ENDPOINT": (
                "https://rustfs.cabotage.svc.cluster.local:9000"
            ),
            "TENANT_POSTGRES_BACKUP_RUSTFS_CA_SECRET_NAME": "operators-ca-crt",
            "TENANT_POSTGRES_BACKUP_RUSTFS_SECRET_NAME": "cnpg-backups-objectstore",
            "TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAME": "rustfs-source",
            "TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAMESPACE": "postgres",
        }
    )


def _reset_tenant_postgres_backups(app):
    app.config.update(
        {
            "TENANT_POSTGRES_BACKUPS_ENABLED": False,
            "TENANT_POSTGRES_BACKUP_PROVIDER": None,
            "TENANT_POSTGRES_BACKUP_BUCKET": None,
            "TENANT_POSTGRES_BACKUP_IRSA_ROLE_ARN": None,
            "TENANT_POSTGRES_BACKUP_PATH_PREFIX": "tenants",
            "TENANT_POSTGRES_BACKUP_PLUGIN_NAME": "barman-cloud.cloudnative-pg.io",
            "TENANT_POSTGRES_BACKUP_RETENTION_POLICY": "30d",
            "TENANT_POSTGRES_BACKUP_SCHEDULE": "0 0 0 * * *",
            "TENANT_POSTGRES_BACKUP_SERVICE_ACCOUNT_NAME": "cnpg-backups",
            "TENANT_POSTGRES_BACKUP_RUSTFS_ENDPOINT": None,
            "TENANT_POSTGRES_BACKUP_RUSTFS_CA_SECRET_NAME": "operators-ca-crt",
            "TENANT_POSTGRES_BACKUP_RUSTFS_SECRET_NAME": "cnpg-backups-objectstore",
            "TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAME": None,
            "TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAMESPACE": None,
        }
    )


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestResourceModels:
    def test_converted_default_environment_namespace_flips_when_new_app_is_enrolled(
        self, app, org
    ):
        project = Project(
            name="Converted Project",
            organization_id=org.id,
            environments_enabled=True,
        )
        db.session.add(project)
        db.session.flush()
        environment = Environment(
            name="default",
            project_id=project.id,
            is_default=True,
        )
        db.session.add(environment)
        db.session.flush()

        legacy_app = Application(
            name="Legacy App",
            slug=f"legacy-app-{uuid.uuid4().hex[:8]}",
            project_id=project.id,
        )
        enrolled_app = Application(
            name="Enrolled App",
            slug=f"enrolled-app-{uuid.uuid4().hex[:8]}",
            project_id=project.id,
        )
        db.session.add(legacy_app)
        db.session.add(enrolled_app)
        db.session.flush()

        db.session.add(
            ApplicationEnvironment(
                application_id=legacy_app.id,
                environment_id=environment.id,
                k8s_identifier=None,
            )
        )
        db.session.add(
            ApplicationEnvironment(
                application_id=enrolled_app.id,
                environment_id=environment.id,
                k8s_identifier=environment.k8s_identifier,
            )
        )
        db.session.flush()

        # Legacy default environments are supposed to remain on the org namespace
        # after enabling environments, even if new env-style enrollments are added.
        assert environment.k8s_namespace == org.k8s_identifier

    def test_environment_namespace_uses_env_namespace_when_explicitly_enabled(
        self, app, org
    ):
        project = Project(
            name="Env Project",
            organization_id=org.id,
            environments_enabled=True,
        )
        db.session.add(project)
        db.session.flush()
        environment = Environment(
            name="production",
            project_id=project.id,
            uses_environment_namespace=True,
        )
        db.session.add(environment)
        db.session.flush()

        assert environment.k8s_namespace == safe_k8s_name(
            org.k8s_identifier, environment.k8s_identifier
        )

    def test_create_postgres_resource(self, app, environment):
        r = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="Main DB",
            size_class="db.medium",
            storage_size=10,
            ha_enabled=False,
            backup_strategy="daily",
            postgres_parameters=compute_postgres_parameters("db.medium"),
        )
        db.session.add(r)
        db.session.flush()

        assert r.id is not None
        assert r.slug == "main-db"
        assert r.k8s_identifier is not None
        assert r.type == "postgres"
        assert r.provisioning_status == "pending"
        assert r.deleted_at is None
        assert r.postgres_parameters["shared_buffers"] == "256MB"

    def test_create_redis_resource(self, app, environment):
        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Cache",
            size_class="cache.small",
            storage_size=1,
            ha_enabled=False,
        )
        db.session.add(r)
        db.session.flush()

        assert r.id is not None
        assert r.slug == "cache"
        assert r.type == "redis"
        assert r.provisioning_status == "pending"
        assert r.leader_replicas == 3
        assert r.follower_replicas == 3

    def test_environment_resources_relationship(self, app, environment):
        pg = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="DB",
            size_class="db.small",
            storage_size=5,
        )
        rd = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Redis",
            size_class="cache.small",
            storage_size=1,
        )
        db.session.add_all([pg, rd])
        db.session.flush()

        assert len(environment.active_resources) == 2
        assert len(environment.active_postgres_resources) == 1
        assert len(environment.active_redis_resources) == 1

    def test_soft_delete_excludes_from_active(self, app, environment):
        r = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="Old DB",
            size_class="db.small",
            storage_size=5,
        )
        db.session.add(r)
        db.session.flush()

        assert len(environment.active_resources) == 1

        r.deleted_at = datetime.datetime.now(datetime.timezone.utc)
        db.session.flush()

        assert len(environment.active_resources) == 0

    def test_polymorphic_identity(self, app, environment):
        pg = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="PG",
            size_class="db.small",
            storage_size=5,
            backup_strategy="streaming",
        )
        rd = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="RD",
            size_class="cache.small",
            storage_size=1,
        )
        db.session.add_all([pg, rd])
        db.session.flush()

        resources = Resource.query.filter_by(environment_id=environment.id).all()
        types = {r.type for r in resources}
        assert types == {"postgres", "redis"}

        pg_loaded = [r for r in resources if r.type == "postgres"][0]
        assert isinstance(pg_loaded, PostgresResource)
        assert pg_loaded.backup_strategy == "streaming"


# ---------------------------------------------------------------------------
# compute_postgres_parameters tests
# ---------------------------------------------------------------------------


class TestPostgresParameterTuning:
    def test_all_size_classes_produce_valid_params(self):
        for name in postgres_size_classes:
            params = compute_postgres_parameters(name)
            assert "shared_buffers" in params
            assert "effective_cache_size" in params
            assert "work_mem" in params
            assert "maintenance_work_mem" in params
            assert "wal_buffers" in params
            assert "max_connections" in params
            assert params["random_page_cost"] == "1.1"
            assert params["checkpoint_completion_target"] == "0.9"

    def test_shared_buffers_is_quarter_of_ram(self):
        params = compute_postgres_parameters("db.small")
        assert params["shared_buffers"] == "128MB"

        params = compute_postgres_parameters("db.2xlarge")
        assert params["shared_buffers"] == "2048MB"

    def test_effective_cache_size_is_three_quarters(self):
        params = compute_postgres_parameters("db.medium")
        assert params["effective_cache_size"] == "768MB"

    def test_work_mem_scales_with_ram(self):
        small = compute_postgres_parameters("db.small")
        xlarge = compute_postgres_parameters("db.xlarge")
        small_val = int(small["work_mem"].replace("kB", ""))
        xlarge_val = int(xlarge["work_mem"].replace("kB", ""))
        assert xlarge_val > small_val

    def test_invalid_size_class_raises(self):
        with pytest.raises(KeyError):
            compute_postgres_parameters("db.nonexistent")


# ---------------------------------------------------------------------------
# View / Route tests
# ---------------------------------------------------------------------------


class TestPostgresRoutes:
    def test_create_postgres_resource(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        resp = client.post(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/postgres/create",
            data={
                "name": "My Database",
                "slug": "my-database",
                "service_version": "18",
                "size_class": "db.medium",
                "storage_size": 10,
                # ha_enabled omitted = False for BooleanField
                "backup_strategy": "daily",
                "environment_id": str(environment.id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/postgres/my-database" in resp.headers["Location"]

        r = PostgresResource.query.filter_by(
            environment_id=environment.id, slug="my-database"
        ).first()
        assert r is not None
        assert r.name == "My Database"
        assert r.size_class == "db.medium"
        assert r.storage_size == 10
        assert r.ha_enabled is False
        assert r.backup_strategy == "daily"
        assert r.postgres_parameters is not None
        assert r.postgres_parameters["shared_buffers"] == "256MB"

    def test_create_postgres_with_ha(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        resp = client.post(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/postgres/create",
            data={
                "name": "HA Database",
                "slug": "ha-database",
                "service_version": "18",
                "size_class": "db.large",
                "storage_size": 50,
                "ha_enabled": "y",
                "backup_strategy": "streaming",
                "environment_id": str(environment.id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

        r = PostgresResource.query.filter_by(
            environment_id=environment.id, slug="ha-database"
        ).first()
        assert r is not None
        assert r.ha_enabled is True
        assert r.backup_strategy == "streaming"

    def test_create_postgres_auto_slug(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        resp = client.post(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/postgres/create",
            data={
                "name": "Primary Database",
                "slug": "",
                "service_version": "18",
                "size_class": "db.small",
                "storage_size": 5,
                "backup_strategy": "daily",
                "environment_id": str(environment.id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

        r = PostgresResource.query.filter_by(environment_id=environment.id).first()
        assert r is not None
        assert r.slug == "primary-database"

    def test_create_postgres_get_renders_form(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/postgres/create"
        )
        assert resp.status_code == 200
        assert b"Add PostgreSQL Database" in resp.data

    def test_postgres_detail_page(self, client, admin_user, org, project, environment):
        _login(client, admin_user)
        r = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="Detail Test",
            slug="detail-test",
            size_class="db.large",
            storage_size=20,
            ha_enabled=True,
            backup_strategy="streaming",
            postgres_parameters=compute_postgres_parameters("db.large"),
        )
        db.session.add(r)
        db.session.commit()

        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/postgres/detail-test"
        )
        assert resp.status_code == 200
        assert b"Detail Test" in resp.data
        assert b"db.large" in resp.data
        assert b"High Availability" in resp.data

    def test_edit_postgres_resource(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        r = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="Edit Test",
            slug="edit-test",
            size_class="db.small",
            storage_size=5,
            backup_strategy="daily",
        )
        db.session.add(r)
        db.session.commit()

        resp = client.post(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/postgres/edit-test/settings",
            data={
                "resource_id": str(r.id),
                "current_storage_size": "5",
                "service_version": "18",
                "size_class": "db.large",
                "storage_size": 20,
                "ha_enabled": "y",
                "backup_strategy": "streaming",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

        db.session.refresh(r)
        assert r.size_class == "db.large"
        assert r.storage_size == 20
        assert r.ha_enabled is True
        assert r.backup_strategy == "streaming"
        assert r.postgres_parameters["shared_buffers"] == "512MB"

    def test_edit_postgres_cannot_reduce_storage(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        r = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="Shrink Test",
            slug="shrink-test",
            size_class="db.small",
            storage_size=20,
            backup_strategy="daily",
        )
        db.session.add(r)
        db.session.commit()

        resp = client.post(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/postgres/shrink-test/settings",
            data={
                "resource_id": str(r.id),
                "current_storage_size": "20",
                "service_version": "18",
                "size_class": "db.small",
                "storage_size": 10,
                "backup_strategy": "daily",
            },
            follow_redirects=False,
        )
        # Should re-render form with error, not redirect
        assert resp.status_code == 200
        assert b"cannot be reduced" in resp.data

    def test_delete_postgres_resource(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        r = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="Delete Me",
            slug="delete-me",
            size_class="db.small",
            storage_size=5,
            backup_strategy="daily",
        )
        db.session.add(r)
        db.session.commit()
        resource_id = str(r.id)

        resp = client.post(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/postgres/delete-me/delete",
            data={
                "resource_id": resource_id,
                "name": "delete-me",
                "confirm": "delete-me",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

        db.session.refresh(r)
        assert r.deleted_at is not None
        assert r.slug.startswith("--deleted-delete-me-")

    def test_unauthenticated_returns_redirect(self, client, org, project, environment):
        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/postgres/create"
        )
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


class TestRedisRoutes:
    def test_create_redis_resource(self, client, admin_user, org, project, environment):
        _login(client, admin_user)
        resp = client.post(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/redis/create",
            data={
                "name": "My Cache",
                "slug": "my-cache",
                "service_version": "8",
                "size_class": "cache.medium",
                "storage_size": 5,
                # ha_enabled omitted = False
                "leader_replicas": 3,
                "follower_replicas": 3,
                "environment_id": str(environment.id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/redis/my-cache" in resp.headers["Location"]

        r = RedisResource.query.filter_by(
            environment_id=environment.id, slug="my-cache"
        ).first()
        assert r is not None
        assert r.name == "My Cache"
        assert r.size_class == "cache.medium"
        assert r.ha_enabled is False
        assert r.leader_replicas == 3
        assert r.follower_replicas == 3

    def test_create_redis_get_renders_form(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/redis/create"
        )
        assert resp.status_code == 200
        assert b"Add Redis Instance" in resp.data
        assert b'id="redis-cluster-fields"' in resp.data
        assert b'id="redis-cluster-fields" class="space-y-4 hidden"' in resp.data

    def test_redis_detail_page(self, client, admin_user, org, project, environment):
        _login(client, admin_user)
        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Cache Detail",
            slug="cache-detail",
            size_class="cache.large",
            storage_size=5,
            ha_enabled=True,
        )
        db.session.add(r)
        db.session.commit()

        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/redis/cache-detail"
        )
        assert resp.status_code == 200
        assert b"Cache Detail" in resp.data
        assert b"cache.large" in resp.data

    def test_edit_redis_resource(self, client, admin_user, org, project, environment):
        _login(client, admin_user)
        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Edit Redis",
            slug="edit-redis",
            size_class="cache.small",
            storage_size=1,
            ha_enabled=True,
        )
        db.session.add(r)
        db.session.commit()

        resp = client.post(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/redis/edit-redis/settings",
            data={
                "resource_id": str(r.id),
                "current_storage_size": "1",
                "service_version": "8",
                "size_class": "cache.xlarge",
                "storage_size": 10,
                "leader_replicas": 5,
                "follower_replicas": 2,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

        db.session.refresh(r)
        assert r.size_class == "cache.xlarge"
        assert r.storage_size == 10
        assert r.ha_enabled is True
        assert r.leader_replicas == 5
        assert r.follower_replicas == 2

    def test_edit_redis_settings_hides_replica_fields_for_standalone(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Standalone Settings",
            slug="standalone-settings",
            size_class="cache.small",
            storage_size=1,
            ha_enabled=False,
        )
        db.session.add(r)
        db.session.commit()

        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/redis/standalone-settings/settings"
        )
        assert resp.status_code == 200
        assert b"Topology is fixed after creation" in resp.data
        assert b"Leader Replicas" not in resp.data
        assert b"Follower Replicas" not in resp.data

    def test_edit_redis_settings_shows_replica_fields_for_ha(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="HA Settings",
            slug="ha-settings",
            size_class="cache.large",
            storage_size=5,
            ha_enabled=True,
        )
        db.session.add(r)
        db.session.commit()

        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/redis/ha-settings/settings"
        )
        assert resp.status_code == 200
        assert b"Leader Replicas" in resp.data
        assert b"Follower Replicas" in resp.data

    def test_edit_redis_cannot_convert_to_ha(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Standalone Redis",
            slug="standalone-redis",
            size_class="cache.small",
            storage_size=1,
            ha_enabled=False,
        )
        db.session.add(r)
        db.session.commit()

        resp = client.post(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/redis/standalone-redis/settings",
            data={
                "resource_id": str(r.id),
                "current_storage_size": "1",
                "size_class": "cache.medium",
                "storage_size": 2,
                "ha_enabled": "y",
                "leader_replicas": 3,
                "follower_replicas": 3,
            },
            follow_redirects=True,
        )
        assert resp.status_code == 400
        assert b"Redis topology cannot be changed after creation" in resp.data

        db.session.refresh(r)
        assert r.ha_enabled is False
        assert r.size_class == "cache.small"
        assert r.storage_size == 1

    def test_edit_redis_cannot_convert_from_ha(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="HA Redis",
            slug="ha-redis",
            size_class="cache.large",
            storage_size=5,
            ha_enabled=True,
        )
        db.session.add(r)
        db.session.commit()

        resp = client.post(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/redis/ha-redis/settings",
            data={
                "resource_id": str(r.id),
                "current_storage_size": "5",
                "size_class": "cache.xlarge",
                "storage_size": 10,
                "ha_enabled": "0",
                "leader_replicas": 3,
                "follower_replicas": 3,
            },
            follow_redirects=True,
        )
        assert resp.status_code == 400
        assert b"Redis topology cannot be changed after creation" in resp.data

        db.session.refresh(r)
        assert r.ha_enabled is True
        assert r.size_class == "cache.large"
        assert r.storage_size == 5

    def test_delete_redis_resource(self, client, admin_user, org, project, environment):
        _login(client, admin_user)
        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Delete Redis",
            slug="delete-redis",
            size_class="cache.small",
            storage_size=1,
        )
        db.session.add(r)
        db.session.commit()
        resource_id = str(r.id)

        resp = client.post(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/redis/delete-redis/delete",
            data={
                "resource_id": resource_id,
                "name": "delete-redis",
                "confirm": "delete-redis",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

        db.session.refresh(r)
        assert r.deleted_at is not None


class TestEnvironmentDashboardServices:
    def test_backing_services_section_visible(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}"
        )
        assert resp.status_code == 200
        assert b"Backing Services" in resp.data
        assert b"Add Service" in resp.data

    def test_resources_shown_in_dashboard(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        pg = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="Main DB",
            slug="main-db",
            size_class="db.medium",
            storage_size=10,
            backup_strategy="daily",
        )
        rd = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="App Cache",
            slug="app-cache",
            size_class="cache.small",
            storage_size=1,
        )
        db.session.add_all([pg, rd])
        db.session.commit()

        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}"
        )
        assert resp.status_code == 200
        assert b"Main DB" in resp.data
        assert b"App Cache" in resp.data


# ---------------------------------------------------------------------------
# Celery task tests (unit-level, mocking K8s)
# ---------------------------------------------------------------------------


class TestCeleryTasks:
    def _mock_k8s_apis(self):
        """Set up mock K8s APIs that return 404 for all GETs (fresh creates)."""
        import base64
        from kubernetes.client.rest import ApiException

        mock_custom_api = MagicMock()
        mock_core_api = MagicMock()
        mock_apps_api = MagicMock()
        mock_rbac_api = MagicMock()

        def _get_custom_object(group, version, namespace, plural, name):
            key = (group, plural, namespace, name)
            seen = getattr(_get_custom_object, "_seen", set())
            if key not in seen:
                seen.add(key)
                _get_custom_object._seen = seen
                raise ApiException(status=404)

            if group == "postgresql.cnpg.io" and plural == "clusters":
                instances = 2 if name.startswith("ha-") else 1
                return {
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "readyInstances": instances,
                        "currentPrimary": f"{name}-1",
                    }
                }
            if group == "redis.redis.opstreelabs.in" and plural == "redisclusters":
                return {
                    "status": {
                        "state": "Ready",
                        "readyLeaderReplicas": 3,
                        "readyFollowerReplicas": 3,
                    }
                }

            raise ApiException(status=404)

        mock_custom_api.get_namespaced_custom_object.side_effect = _get_custom_object

        def _read_secret(name, namespace):
            if name == "operators-ca-crt":
                secret = MagicMock()
                secret.type = "Opaque"
                secret.data = {"ca.crt": base64.b64encode(b"fake-ca").decode()}
                return secret
            if name.endswith("-app"):
                secret = MagicMock()
                secret.data = {
                    "password": base64.b64encode(b"pgpassword").decode(),
                    "username": base64.b64encode(b"app").decode(),
                }
                return secret
            if "-password" in name:
                if not hasattr(_read_secret, f"_seen_{name}"):
                    setattr(_read_secret, f"_seen_{name}", True)
                    raise ApiException(status=404)
                secret = MagicMock()
                secret.data = {"password": base64.b64encode(b"testpassword").decode()}
                return secret
            raise ApiException(status=404)

        mock_core_api.read_namespaced_secret.side_effect = _read_secret

        ready_pod = MagicMock()
        ready_pod.metadata.deletion_timestamp = None
        ready_pod.status.phase = "Running"
        ready_pod.status.conditions = [MagicMock(type="Ready", status="True")]
        ready_pod.status.container_statuses = []
        mock_core_api.read_namespaced_pod.return_value = ready_pod

        def _read_statefulset(name, namespace):
            statefulset = MagicMock()
            statefulset.spec.template.metadata.annotations = {
                "redis.opstreelabs.in": "true",
            }
            return statefulset

        mock_apps_api.read_namespaced_stateful_set.side_effect = _read_statefulset
        mock_rbac_api.read_namespaced_role_binding.side_effect = ApiException(
            status=404
        )
        return mock_custom_api, mock_core_api, mock_apps_api, mock_rbac_api

    def test_reconcile_postgres_creates_crd(self, app, environment):
        from cabotage.celery.tasks.resources import _reconcile_postgres

        r = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="Task Test PG",
            size_class="db.small",
            storage_size=5,
            backup_strategy="daily",
            postgres_parameters=compute_postgres_parameters("db.small"),
        )
        db.session.add(r)
        db.session.commit()

        (
            mock_custom_api,
            mock_core_api,
            mock_apps_api,
            mock_rbac_api,
        ) = self._mock_k8s_apis()
        _reconcile_postgres(
            r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
        )

        create_calls = mock_custom_api.create_namespaced_custom_object.call_args_list
        assert len(create_calls) == 2
        assert create_calls[0][0][0] == "cert-manager.io"
        assert create_calls[1][0][0] == "postgresql.cnpg.io"

        body = create_calls[1][0][4]
        assert body["kind"] == "Cluster"
        assert body["spec"]["instances"] == 1
        assert body["spec"]["storage"]["size"] == "5Gi"
        assert "certificates" in body["spec"]

        assert r.provisioning_status == "ready"
        assert r.connection_info["port"] == "5432"
        assert r.connection_info["sslmode"] == "verify-full"

    def test_reconcile_postgres_with_backups_creates_object_store_and_schedule(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _reconcile_postgres

        _configure_s3_tenant_postgres_backups(app)
        try:
            r = PostgresResource(
                service_version="18",
                environment_id=environment.id,
                name="Backed Up PG",
                size_class="db.small",
                storage_size=5,
                backup_strategy="daily",
                postgres_parameters=compute_postgres_parameters("db.small"),
            )
            db.session.add(r)
            db.session.commit()

            (
                mock_custom_api,
                mock_core_api,
                mock_apps_api,
                mock_rbac_api,
            ) = self._mock_k8s_apis()

            def _get_custom_object(group, version, namespace, plural, name):
                key = (group, plural, namespace, name)
                seen = getattr(_get_custom_object, "_seen", set())
                if key not in seen:
                    seen.add(key)
                    _get_custom_object._seen = seen
                    raise ApiException(status=404)

                if group == "postgresql.cnpg.io" and plural == "clusters":
                    return {
                        "status": {
                            "conditions": [
                                {"type": "Ready", "status": "True"},
                                {
                                    "type": "ContinuousArchiving",
                                    "status": "True",
                                },
                            ],
                            "pluginStatus": [
                                {"name": "barman-cloud.cloudnative-pg.io"}
                            ],
                            "readyInstances": 1,
                            "currentPrimary": f"{name}-1",
                        }
                    }

                raise ApiException(status=404)

            mock_custom_api.get_namespaced_custom_object.side_effect = (
                _get_custom_object
            )

            _reconcile_postgres(
                r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
            )

            create_calls = (
                mock_custom_api.create_namespaced_custom_object.call_args_list
            )
            created_plurals = [call[0][3] for call in create_calls]
            assert created_plurals == [
                "objectstores",
                "certificates",
                "clusters",
                "scheduledbackups",
            ]

            object_store_body = create_calls[0][0][4]
            cluster_body = create_calls[2][0][4]
            scheduled_backup_body = create_calls[3][0][4]

            assert object_store_body["kind"] == "ObjectStore"
            assert (
                object_store_body["spec"]["configuration"]["s3Credentials"][
                    "inheritFromIAMRole"
                ]
                is True
            )
            assert cluster_body["spec"]["serviceAccountName"] == "cnpg-backups"
            assert cluster_body["spec"]["plugins"][0]["parameters"][
                "barmanObjectName"
            ].endswith("-backups")
            assert "isWALArchiver" not in cluster_body["spec"]["plugins"][0]
            assert scheduled_backup_body["kind"] == "ScheduledBackup"
            assert scheduled_backup_body["spec"]["immediate"] is True
            assert scheduled_backup_body["spec"]["pluginConfiguration"]["name"] == (
                "barman-cloud.cloudnative-pg.io"
            )
            mock_core_api.replace_namespaced_service_account.assert_called_once()
            assert r.provisioning_status == "ready"
        finally:
            _reset_tenant_postgres_backups(app)

    def test_reconcile_postgres_daily_backups_do_not_require_continuous_archiving(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _reconcile_postgres

        _configure_s3_tenant_postgres_backups(app)
        try:
            r = PostgresResource(
                service_version="18",
                environment_id=environment.id,
                name="Daily Backup PG",
                size_class="db.small",
                storage_size=5,
                backup_strategy="daily",
                postgres_parameters=compute_postgres_parameters("db.small"),
            )
            db.session.add(r)
            db.session.commit()

            (
                mock_custom_api,
                mock_core_api,
                mock_apps_api,
                mock_rbac_api,
            ) = self._mock_k8s_apis()

            def _get_custom_object(group, version, namespace, plural, name):
                key = (group, plural, namespace, name)
                seen = getattr(_get_custom_object, "_seen", set())
                if key not in seen:
                    seen.add(key)
                    _get_custom_object._seen = seen
                    raise ApiException(status=404)

                if group == "postgresql.cnpg.io" and plural == "clusters":
                    return {
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "pluginStatus": [
                                {"name": "barman-cloud.cloudnative-pg.io"}
                            ],
                            "readyInstances": 1,
                            "currentPrimary": f"{name}-1",
                        }
                    }

                raise ApiException(status=404)

            mock_custom_api.get_namespaced_custom_object.side_effect = (
                _get_custom_object
            )

            _reconcile_postgres(
                r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
            )

            created_plurals = [
                call[0][3]
                for call in mock_custom_api.create_namespaced_custom_object.call_args_list
            ]
            assert created_plurals == [
                "objectstores",
                "certificates",
                "clusters",
                "scheduledbackups",
            ]
            assert r.provisioning_status == "ready"
        finally:
            _reset_tenant_postgres_backups(app)

    def test_reconcile_postgres_streaming_waits_for_continuous_archiving_before_schedule(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _reconcile_postgres

        _configure_s3_tenant_postgres_backups(app)
        try:
            r = PostgresResource(
                service_version="18",
                environment_id=environment.id,
                name="Pending Streaming Backup PG",
                size_class="db.small",
                storage_size=5,
                backup_strategy="streaming",
                postgres_parameters=compute_postgres_parameters("db.small"),
            )
            db.session.add(r)
            db.session.commit()

            (
                mock_custom_api,
                mock_core_api,
                mock_apps_api,
                mock_rbac_api,
            ) = self._mock_k8s_apis()

            def _get_custom_object(group, version, namespace, plural, name):
                key = (group, plural, namespace, name)
                seen = getattr(_get_custom_object, "_seen", set())
                if key not in seen:
                    seen.add(key)
                    _get_custom_object._seen = seen
                    raise ApiException(status=404)

                if group == "postgresql.cnpg.io" and plural == "clusters":
                    return {
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "readyInstances": 1,
                            "currentPrimary": f"{name}-1",
                        }
                    }

                raise ApiException(status=404)

            mock_custom_api.get_namespaced_custom_object.side_effect = (
                _get_custom_object
            )

            _reconcile_postgres(
                r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
            )

            created_plurals = [
                call[0][3]
                for call in mock_custom_api.create_namespaced_custom_object.call_args_list
            ]
            assert created_plurals == ["objectstores", "certificates", "clusters"]
            assert r.provisioning_status == "ready"
        finally:
            _reset_tenant_postgres_backups(app)

    def test_reconcile_postgres_with_rustfs_backups_copies_secret(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _reconcile_postgres

        _configure_rustfs_tenant_postgres_backups(app)
        try:
            r = PostgresResource(
                service_version="18",
                environment_id=environment.id,
                name="RustFS Backup PG",
                size_class="db.small",
                storage_size=5,
                backup_strategy="daily",
                postgres_parameters=compute_postgres_parameters("db.small"),
            )
            db.session.add(r)
            db.session.commit()

            (
                mock_custom_api,
                mock_core_api,
                mock_apps_api,
                mock_rbac_api,
            ) = self._mock_k8s_apis()
            base_read_secret = mock_core_api.read_namespaced_secret.side_effect

            def _read_secret(name, namespace):
                if name == "rustfs-source" and namespace == "postgres":
                    secret = MagicMock()
                    secret.type = "Opaque"
                    secret.data = {
                        "access-key-id": "YWNjZXNz",
                        "secret-key": "c2VjcmV0",
                        "region": "dXMtZWFzdC0x",
                    }
                    return secret
                return base_read_secret(name, namespace)

            mock_core_api.read_namespaced_secret.side_effect = _read_secret

            _reconcile_postgres(
                r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
            )

            replaced_secret_names = [
                call[0][0]
                for call in mock_core_api.replace_namespaced_secret.call_args_list
            ]
            assert "cnpg-backups-objectstore" in replaced_secret_names
        finally:
            _reset_tenant_postgres_backups(app)

    def test_reconcile_postgres_repairs_barman_rolebinding_subject(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _reconcile_postgres

        _configure_s3_tenant_postgres_backups(app)
        try:
            r = PostgresResource(
                service_version="18",
                environment_id=environment.id,
                name="Migrated Backup PG",
                size_class="db.small",
                storage_size=5,
                backup_strategy="daily",
                postgres_parameters=compute_postgres_parameters("db.small"),
            )
            db.session.add(r)
            db.session.commit()

            (
                mock_custom_api,
                mock_core_api,
                mock_apps_api,
                mock_rbac_api,
            ) = self._mock_k8s_apis()

            rolebinding = MagicMock()
            rolebinding.subjects = [
                MagicMock(
                    kind="ServiceAccount",
                    name="old-cluster-name",
                    namespace=environment.k8s_namespace,
                )
            ]
            mock_rbac_api.read_namespaced_role_binding.side_effect = None
            mock_rbac_api.read_namespaced_role_binding.return_value = rolebinding

            _reconcile_postgres(
                r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
            )

            patch_call = mock_rbac_api.patch_namespaced_role_binding.call_args
            assert patch_call[0][0].endswith("-barman-cloud")
            assert patch_call[0][1] == environment.k8s_namespace
            assert patch_call[0][2]["subjects"] == [
                {
                    "kind": "ServiceAccount",
                    "name": "cnpg-backups",
                    "namespace": environment.k8s_namespace,
                }
            ]
        finally:
            _reset_tenant_postgres_backups(app)

    def test_reconcile_postgres_patches_scheduled_backup_without_immediate(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _reconcile_postgres

        _configure_s3_tenant_postgres_backups(app)
        try:
            r = PostgresResource(
                service_version="18",
                environment_id=environment.id,
                name="Existing Schedule PG",
                size_class="db.small",
                storage_size=5,
                backup_strategy="daily",
                postgres_parameters=compute_postgres_parameters("db.small"),
            )
            db.session.add(r)
            db.session.commit()

            (
                mock_custom_api,
                mock_core_api,
                mock_apps_api,
                mock_rbac_api,
            ) = self._mock_k8s_apis()

            def _get_custom_object(group, version, namespace, plural, name):
                if group == "cert-manager.io":
                    raise ApiException(status=404)
                if group == "postgresql.cnpg.io" and plural == "clusters":
                    return {
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "pluginStatus": [
                                {"name": "barman-cloud.cloudnative-pg.io"}
                            ],
                            "readyInstances": 1,
                            "currentPrimary": f"{name}-1",
                        }
                    }
                if group == "barmancloud.cnpg.io" and plural == "objectstores":
                    return {"metadata": {"name": name}}
                if group == "postgresql.cnpg.io" and plural == "scheduledbackups":
                    return {"metadata": {"name": name}}
                raise ApiException(status=404)

            mock_custom_api.get_namespaced_custom_object.side_effect = (
                _get_custom_object
            )

            _reconcile_postgres(
                r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
            )

            patch_calls = mock_custom_api.patch_namespaced_custom_object.call_args_list
            scheduled_backup_patch = [
                call for call in patch_calls if call[0][3] == "scheduledbackups"
            ][0]
            assert "immediate" not in scheduled_backup_patch[0][5]["spec"]
        finally:
            _reset_tenant_postgres_backups(app)

    def test_reconcile_postgres_stays_provisioning_until_cluster_ready(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _reconcile_postgres

        r = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="Pending PG",
            size_class="db.small",
            storage_size=5,
            backup_strategy="daily",
            postgres_parameters=compute_postgres_parameters("db.small"),
        )
        db.session.add(r)
        db.session.commit()

        (
            mock_custom_api,
            mock_core_api,
            mock_apps_api,
            mock_rbac_api,
        ) = self._mock_k8s_apis()

        def _get_custom_object(group, version, namespace, plural, name):
            if group == "cert-manager.io":
                raise ApiException(status=404)
            if group == "postgresql.cnpg.io" and plural == "clusters":
                return {"status": {"readyInstances": 0, "currentPrimary": None}}
            raise ApiException(status=404)

        mock_custom_api.get_namespaced_custom_object.side_effect = _get_custom_object

        _reconcile_postgres(
            r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
        )

        assert r.provisioning_status == "provisioning"
        assert r.provisioning_error is None

    def test_reconcile_redis_standalone(self, app, environment):
        from cabotage.celery.tasks.resources import _reconcile_redis, _resource_k8s_name

        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Task Test Redis",
            size_class="cache.medium",
            storage_size=2,
            ha_enabled=False,
        )
        db.session.add(r)
        db.session.commit()

        (
            mock_custom_api,
            mock_core_api,
            mock_apps_api,
            mock_rbac_api,
        ) = self._mock_k8s_apis()
        _reconcile_redis(
            r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
        )

        create_calls = mock_custom_api.create_namespaced_custom_object.call_args_list
        assert len(create_calls) == 2
        assert create_calls[1][0][0] == "redis.redis.opstreelabs.in"
        assert create_calls[1][0][3] == "redis"

        body = create_calls[1][0][4]
        assert body["kind"] == "Redis"
        assert body["spec"]["TLS"]["secret"]["secretName"].endswith("-tls")
        assert "redisSecret" in body["spec"]["kubernetesConfig"]
        mock_core_api.create_namespaced_secret.assert_called_once()

        assert r.provisioning_status == "ready"
        assert (
            r.connection_info["host"]
            == f"{_resource_k8s_name(r)}.{environment.k8s_namespace}.svc.cluster.local"
        )
        assert r.connection_info["tls"] is True

    def test_reconcile_redis_standalone_stays_provisioning_until_pod_ready(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _reconcile_redis

        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Slow Redis",
            size_class="cache.medium",
            storage_size=2,
            ha_enabled=False,
        )
        db.session.add(r)
        db.session.commit()

        (
            mock_custom_api,
            mock_core_api,
            mock_apps_api,
            mock_rbac_api,
        ) = self._mock_k8s_apis()
        pending_pod = MagicMock()
        pending_pod.metadata.deletion_timestamp = None
        pending_pod.status.phase = "Pending"
        pending_pod.status.conditions = [MagicMock(type="Ready", status="False")]
        pending_pod.status.container_statuses = []
        mock_core_api.read_namespaced_pod.return_value = pending_pod

        _reconcile_redis(
            r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
        )

        assert r.provisioning_status == "provisioning"
        assert r.provisioning_error is None

    def test_reconcile_redis_cluster(self, app, environment):
        from cabotage.celery.tasks.resources import _reconcile_redis, _resource_k8s_name

        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="HA Redis",
            size_class="cache.large",
            storage_size=5,
            ha_enabled=True,
        )
        db.session.add(r)
        db.session.commit()

        (
            mock_custom_api,
            mock_core_api,
            mock_apps_api,
            mock_rbac_api,
        ) = self._mock_k8s_apis()
        _reconcile_redis(
            r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
        )

        create_calls = mock_custom_api.create_namespaced_custom_object.call_args_list
        cluster_call = create_calls[-1]
        assert cluster_call[0][3] == "redisclusters"
        body = cluster_call[0][4]
        assert body["kind"] == "RedisCluster"
        assert body["spec"]["clusterSize"] == 3
        assert body["spec"]["redisLeader"]["replicas"] == 3
        assert body["spec"]["redisFollower"]["replicas"] == 3
        assert (
            r.connection_info["host"]
            == f"{_resource_k8s_name(r)}-master.{environment.k8s_namespace}.svc.cluster.local"
        )
        assert r.connection_info["client_mode"] == "cluster-aware"
        assert (
            r.connection_info["startup_nodes"]
            == f"{_resource_k8s_name(r)}-master.{environment.k8s_namespace}.svc.cluster.local:6379"
        )

        env_configs = {
            cfg.name: cfg
            for cfg in EnvironmentConfiguration.query.filter_by(resource_id=r.id).all()
        }
        slug_upper = r.slug.upper().replace("-", "_")
        assert (
            env_configs[f"{slug_upper}_REDIS_HOST"].value
            == f"{_resource_k8s_name(r)}-master.{environment.k8s_namespace}.svc.cluster.local"
        )
        assert env_configs[f"{slug_upper}_REDIS_CLUSTER"].value == "true"
        assert (
            env_configs[f"{slug_upper}_REDIS_STARTUP_NODES"].value
            == f"{_resource_k8s_name(r)}-master.{environment.k8s_namespace}.svc.cluster.local:6379"
        )
        assert r.provisioning_status == "ready"

    def test_reconcile_redis_cluster_uses_custom_replica_counts(self, app, environment):
        from cabotage.celery.tasks.resources import _reconcile_redis

        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Custom HA Redis",
            size_class="cache.large",
            storage_size=5,
            ha_enabled=True,
            leader_replicas=4,
            follower_replicas=2,
        )
        db.session.add(r)
        db.session.commit()

        (
            mock_custom_api,
            mock_core_api,
            mock_apps_api,
            mock_rbac_api,
        ) = self._mock_k8s_apis()

        def _get_custom_object(group, version, namespace, plural, name):
            key = (group, plural, namespace, name)
            seen = getattr(_get_custom_object, "_seen", set())
            if key not in seen:
                seen.add(key)
                _get_custom_object._seen = seen
                raise ApiException(status=404)
            if group == "redis.redis.opstreelabs.in" and plural == "redisclusters":
                return {
                    "status": {
                        "state": "Ready",
                        "readyLeaderReplicas": 4,
                        "readyFollowerReplicas": 2,
                    }
                }
            raise ApiException(status=404)

        mock_custom_api.get_namespaced_custom_object.side_effect = _get_custom_object

        _reconcile_redis(
            r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
        )

        body = mock_custom_api.create_namespaced_custom_object.call_args_list[-1][0][4]
        assert body["spec"]["clusterSize"] == 4
        assert body["spec"]["redisLeader"]["replicas"] == 4
        assert body["spec"]["redisFollower"]["replicas"] == 2
        assert r.provisioning_status == "ready"

    def test_reconcile_redis_cluster_stays_provisioning_until_operator_ready(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _reconcile_redis

        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Pending HA Redis",
            size_class="cache.large",
            storage_size=5,
            ha_enabled=True,
        )
        db.session.add(r)
        db.session.commit()

        (
            mock_custom_api,
            mock_core_api,
            mock_apps_api,
            mock_rbac_api,
        ) = self._mock_k8s_apis()

        def _get_custom_object(group, version, namespace, plural, name):
            if group == "cert-manager.io":
                raise ApiException(status=404)
            if group == "redis.redis.opstreelabs.in" and plural == "redisclusters":
                return {
                    "status": {
                        "state": "Initializing",
                        "readyLeaderReplicas": 1,
                        "readyFollowerReplicas": 0,
                    }
                }
            raise ApiException(status=404)

        mock_custom_api.get_namespaced_custom_object.side_effect = _get_custom_object

        _reconcile_redis(
            r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
        )

        assert r.provisioning_status == "provisioning"
        assert r.provisioning_error is None

    def test_reconcile_redis_standalone_marks_error_on_crash_loop(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _reconcile_redis

        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Broken Redis",
            size_class="cache.medium",
            storage_size=2,
            ha_enabled=False,
        )
        db.session.add(r)
        db.session.commit()

        (
            mock_custom_api,
            mock_core_api,
            mock_apps_api,
            mock_rbac_api,
        ) = self._mock_k8s_apis()
        crashed_pod = MagicMock()
        crashed_pod.metadata.deletion_timestamp = None
        crashed_pod.status.phase = "Running"
        crashed_pod.status.conditions = [MagicMock(type="Ready", status="False")]
        crashed_pod.status.container_statuses = [
            MagicMock(
                state=MagicMock(
                    waiting=MagicMock(reason="CrashLoopBackOff"),
                    terminated=None,
                )
            )
        ]
        mock_core_api.read_namespaced_pod.return_value = crashed_pod

        _reconcile_redis(
            r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
        )

        assert r.provisioning_status == "error"
        assert "CrashLoopBackOff" in r.provisioning_error

    def test_reconcile_postgres_error_raises(self, app, environment):
        from cabotage.celery.tasks.resources import _reconcile_postgres

        r = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="Fail PG",
            size_class="db.small",
            storage_size=5,
            backup_strategy="daily",
            postgres_parameters=compute_postgres_parameters("db.small"),
        )
        db.session.add(r)
        db.session.commit()

        mock_custom_api = MagicMock()
        mock_core_api = MagicMock()
        mock_apps_api = MagicMock()
        mock_rbac_api = MagicMock()
        mock_core_api.read_namespaced_secret.side_effect = Exception("K8s unreachable")

        with pytest.raises(Exception, match="K8s unreachable"):
            _reconcile_postgres(
                r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
            )

    def test_delete_postgres(self, app, environment):
        from cabotage.celery.tasks.resources import _delete_postgres

        r = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="Delete PG",
            size_class="db.small",
            storage_size=5,
            backup_strategy="daily",
        )
        r.deleted_at = datetime.datetime.now(datetime.timezone.utc)
        db.session.add(r)
        db.session.commit()

        mock_custom_api = MagicMock()
        mock_core_api = MagicMock()
        mock_apps_api = MagicMock()
        mock_rbac_api = MagicMock()
        _delete_postgres(
            r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
        )

        assert mock_custom_api.delete_namespaced_custom_object.call_count == 4
        mock_core_api.delete_namespaced_secret.assert_called_once()

    def test_delete_redis(self, app, environment):
        from cabotage.celery.tasks.resources import _delete_redis

        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Delete Redis",
            size_class="cache.small",
            storage_size=1,
        )
        r.deleted_at = datetime.datetime.now(datetime.timezone.utc)
        db.session.add(r)
        db.session.commit()

        mock_custom_api = MagicMock()
        mock_core_api = MagicMock()
        mock_apps_api = MagicMock()
        mock_rbac_api = MagicMock()
        _delete_redis(r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api)

        assert mock_custom_api.delete_namespaced_custom_object.call_count == 3
        assert mock_core_api.delete_namespaced_secret.call_count == 2

    def test_reconcile_redis_standalone_patches_statefulset_for_backing_pool(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _reconcile_redis

        app.config["BACKING_SERVICES_POOL"] = "backing-services"
        try:
            r = RedisResource(
                service_version="8",
                environment_id=environment.id,
                name="Placed Redis",
                size_class="cache.medium",
                storage_size=2,
                ha_enabled=False,
            )
            db.session.add(r)
            db.session.commit()

            (
                mock_custom_api,
                mock_core_api,
                mock_apps_api,
                mock_rbac_api,
            ) = self._mock_k8s_apis()
            _reconcile_redis(
                r, mock_core_api, mock_custom_api, mock_apps_api, mock_rbac_api
            )

            patch_body = mock_apps_api.patch_namespaced_stateful_set.call_args[0][2]
            assert (
                patch_body["spec"]["template"]["metadata"]["annotations"][
                    "karpenter.sh/do-not-disrupt"
                ]
                == "true"
            )
        finally:
            app.config.pop("BACKING_SERVICES_POOL", None)


# ---------------------------------------------------------------------------
# CRD rendering tests
# ---------------------------------------------------------------------------


class TestCRDRendering:
    def test_cnpg_cluster_standalone(self, app, environment):
        from cabotage.celery.tasks.resources import _render_cnpg_cluster

        r = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="Render Test",
            size_class="db.medium",
            storage_size=10,
            ha_enabled=False,
            backup_strategy="daily",
            postgres_parameters=compute_postgres_parameters("db.medium"),
        )
        db.session.add(r)
        db.session.flush()

        crd = _render_cnpg_cluster(r)
        assert crd["apiVersion"] == "postgresql.cnpg.io/v1"
        assert crd["kind"] == "Cluster"
        assert crd["spec"]["instances"] == 1
        assert crd["spec"]["storage"]["size"] == "10Gi"
        assert crd["spec"]["resources"]["requests"]["cpu"] == "500m"
        assert crd["spec"]["resources"]["limits"]["memory"] == "1Gi"
        assert crd["spec"]["postgresql"]["parameters"]["shared_buffers"] == "256MB"
        # TLS configured
        assert "certificates" in crd["spec"]
        assert crd["spec"]["certificates"]["serverCASecret"] == "operators-ca-crt"
        assert crd["spec"]["certificates"]["serverTLSSecret"].endswith("-tls")
        assert "plugins" not in crd["spec"]

    def test_cnpg_cluster_adds_backing_services_pool_placement(self, app, environment):
        from cabotage.celery.tasks.resources import _render_cnpg_cluster

        app.config["BACKING_SERVICES_POOL"] = "backing-services"
        try:
            r = PostgresResource(
                service_version="18",
                environment_id=environment.id,
                name="Placed PG",
                size_class="db.large",
                storage_size=50,
                ha_enabled=True,
                backup_strategy="streaming",
                postgres_parameters=compute_postgres_parameters("db.large"),
            )
            db.session.add(r)
            db.session.flush()

            crd = _render_cnpg_cluster(r)
            assert (
                crd["spec"]["inheritedMetadata"]["annotations"][
                    "karpenter.sh/do-not-disrupt"
                ]
                == "true"
            )
            assert crd["spec"]["affinity"]["nodeSelector"] == {
                "cabotage.dev/node-pool": "backing-services"
            }
            assert (
                crd["spec"]["affinity"]["tolerations"][0]["value"] == "backing-services"
            )
            assert crd["spec"]["affinity"]["podAntiAffinityType"] == "required"
            assert crd["spec"]["affinity"]["topologyKey"] == "kubernetes.io/hostname"
            assert (
                crd["spec"]["affinity"]["additionalPodAntiAffinity"][
                    "preferredDuringSchedulingIgnoredDuringExecution"
                ][0]["podAffinityTerm"]["labelSelector"]["matchLabels"][
                    "resident-pod.cabotage.io"
                ]
                == "true"
            )
            assert crd["spec"]["affinity"]["additionalPodAntiAffinity"][
                "preferredDuringSchedulingIgnoredDuringExecution"
            ][1]["podAffinityTerm"]["labelSelector"]["matchLabels"][
                "cabotage.io/resource-id"
            ] == str(r.id)
            assert (
                crd["spec"]["affinity"]["additionalPodAntiAffinity"][
                    "preferredDuringSchedulingIgnoredDuringExecution"
                ][1]["podAffinityTerm"]["topologyKey"]
                == "topology.kubernetes.io/zone"
            )
        finally:
            app.config.pop("BACKING_SERVICES_POOL", None)

    def test_cnpg_cluster_ha(self, app, environment):
        from cabotage.celery.tasks.resources import _render_cnpg_cluster

        r = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="HA Test",
            size_class="db.large",
            storage_size=50,
            ha_enabled=True,
            backup_strategy="streaming",
            postgres_parameters=compute_postgres_parameters("db.large"),
        )
        db.session.add(r)
        db.session.flush()

        crd = _render_cnpg_cluster(r)
        assert crd["spec"]["instances"] == 2
        assert "plugins" not in crd["spec"]

    def test_cnpg_cluster_no_backup(self, app, environment):
        from cabotage.celery.tasks.resources import _render_cnpg_cluster

        r = PostgresResource(
            service_version="18",
            environment_id=environment.id,
            name="No Backup",
            size_class="db.small",
            storage_size=5,
            ha_enabled=False,
            backup_strategy="none",
            postgres_parameters=compute_postgres_parameters("db.small"),
        )
        db.session.add(r)
        db.session.flush()

        crd = _render_cnpg_cluster(r)
        assert "plugins" not in crd["spec"]

    def test_cnpg_cluster_adds_backup_plugin_when_enabled(self, app, environment):
        from cabotage.celery.tasks.resources import (
            _render_cnpg_cluster,
            _resource_k8s_name,
        )

        _configure_s3_tenant_postgres_backups(app)
        try:
            r = PostgresResource(
                service_version="18",
                environment_id=environment.id,
                name="Backed Up DB",
                size_class="db.medium",
                storage_size=10,
                ha_enabled=True,
                backup_strategy="daily",
                postgres_parameters=compute_postgres_parameters("db.medium"),
            )
            db.session.add(r)
            db.session.flush()

            crd = _render_cnpg_cluster(r)
            assert crd["spec"]["serviceAccountName"] == "cnpg-backups"
            assert crd["spec"]["plugins"] == [
                {
                    "name": "barman-cloud.cloudnative-pg.io",
                    "parameters": {
                        "barmanObjectName": f"{_resource_k8s_name(r)}-backups",
                        "serverName": _resource_k8s_name(r),
                    },
                }
            ]
        finally:
            _reset_tenant_postgres_backups(app)

    def test_cnpg_cluster_streaming_backup_enables_wal_archiver(self, app, environment):
        from cabotage.celery.tasks.resources import _render_cnpg_cluster

        _configure_s3_tenant_postgres_backups(app)
        try:
            r = PostgresResource(
                service_version="18",
                environment_id=environment.id,
                name="Streaming Backups DB",
                size_class="db.medium",
                storage_size=10,
                ha_enabled=True,
                backup_strategy="streaming",
                postgres_parameters=compute_postgres_parameters("db.medium"),
            )
            db.session.add(r)
            db.session.flush()

            crd = _render_cnpg_cluster(r)
            assert crd["spec"]["plugins"][0]["isWALArchiver"] is True
        finally:
            _reset_tenant_postgres_backups(app)

    def test_postgres_object_store_s3(self, app, environment):
        from cabotage.celery.tasks.resources import (
            _render_postgres_object_store,
            _resource_k8s_name,
        )

        _configure_s3_tenant_postgres_backups(app)
        try:
            r = PostgresResource(
                service_version="18",
                environment_id=environment.id,
                name="S3 Backups",
                size_class="db.small",
                storage_size=5,
                backup_strategy="daily",
                postgres_parameters=compute_postgres_parameters("db.small"),
            )
            db.session.add(r)
            db.session.flush()

            body = _render_postgres_object_store(
                r,
                {
                    "provider": "s3",
                    "bucket": "tenant-postgres-backups",
                    "irsa_role_arn": "arn:aws:iam::123456789012:role/tenant-postgres-backups",
                    "path_prefix": "tenants",
                    "plugin_name": "barman-cloud.cloudnative-pg.io",
                    "retention_policy": "30d",
                    "schedule": "0 0 0 * * *",
                    "service_account_name": "cnpg-backups",
                    "rustfs_endpoint": None,
                    "rustfs_ca_secret_name": "operators-ca-crt",
                    "rustfs_secret_name": "cnpg-backups-objectstore",
                    "rustfs_source_secret_name": None,
                    "rustfs_source_secret_namespace": None,
                },
            )

            assert body["kind"] == "ObjectStore"
            assert body["spec"]["retentionPolicy"] == "30d"
            assert body["spec"]["configuration"]["destinationPath"].endswith(
                f"/{environment.k8s_namespace}/{_resource_k8s_name(r)}/"
            )
            assert body["spec"]["configuration"]["s3Credentials"] == {
                "inheritFromIAMRole": True
            }
        finally:
            _reset_tenant_postgres_backups(app)

    def test_postgres_object_store_rustfs(self, app, environment):
        from cabotage.celery.tasks.resources import _render_postgres_object_store

        _configure_rustfs_tenant_postgres_backups(app)
        try:
            r = PostgresResource(
                service_version="18",
                environment_id=environment.id,
                name="RustFS Backups",
                size_class="db.small",
                storage_size=5,
                backup_strategy="daily",
                postgres_parameters=compute_postgres_parameters("db.small"),
            )
            db.session.add(r)
            db.session.flush()

            body = _render_postgres_object_store(
                r,
                {
                    "provider": "rustfs",
                    "bucket": "cabotage-postgres-backups",
                    "irsa_role_arn": None,
                    "path_prefix": "tenants",
                    "plugin_name": "barman-cloud.cloudnative-pg.io",
                    "retention_policy": "30d",
                    "schedule": "0 0 0 * * *",
                    "service_account_name": "cnpg-backups",
                    "rustfs_endpoint": "https://rustfs.cabotage.svc.cluster.local:9000",
                    "rustfs_ca_secret_name": "operators-ca-crt",
                    "rustfs_secret_name": "cnpg-backups-objectstore",
                    "rustfs_source_secret_name": "rustfs-source",
                    "rustfs_source_secret_namespace": "postgres",
                },
            )

            assert body["spec"]["configuration"]["endpointURL"] == (
                "https://rustfs.cabotage.svc.cluster.local:9000"
            )
            assert body["spec"]["configuration"]["endpointCA"] == {
                "name": "operators-ca-crt",
                "key": "ca.crt",
            }
            assert body["spec"]["configuration"]["s3Credentials"]["accessKeyId"] == {
                "name": "cnpg-backups-objectstore",
                "key": "access-key-id",
            }
        finally:
            _reset_tenant_postgres_backups(app)

    def test_redis_standalone_crd(self, app, environment):
        from cabotage.celery.tasks.resources import _render_redis_standalone

        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Standalone",
            size_class="cache.medium",
            storage_size=2,
            ha_enabled=False,
        )
        db.session.add(r)
        db.session.flush()

        crd = _render_redis_standalone(r)
        assert crd["apiVersion"] == "redis.redis.opstreelabs.in/v1beta2"
        assert crd["kind"] == "Redis"
        assert crd["spec"]["kubernetesConfig"]["resources"]["requests"]["cpu"] == "250m"
        assert (
            crd["spec"]["storage"]["volumeClaimTemplate"]["spec"]["resources"][
                "requests"
            ]["storage"]
            == "2Gi"
        )
        # TLS configured
        assert crd["spec"]["TLS"]["secret"]["secretName"].endswith("-tls")
        # Password configured
        assert crd["spec"]["kubernetesConfig"]["redisSecret"]["key"] == "password"
        assert "nodeSelector" not in crd["spec"]
        assert "tolerations" not in crd["spec"]

    def test_redis_standalone_crd_adds_backing_services_pool_placement(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _render_redis_standalone

        app.config["BACKING_SERVICES_POOL"] = "backing-services"
        try:
            r = RedisResource(
                service_version="8",
                environment_id=environment.id,
                name="Placed Standalone",
                size_class="cache.medium",
                storage_size=2,
                ha_enabled=False,
            )
            db.session.add(r)
            db.session.flush()

            crd = _render_redis_standalone(r)
            assert crd["spec"]["nodeSelector"] == {
                "cabotage.dev/node-pool": "backing-services"
            }
            assert crd["spec"]["tolerations"][0]["value"] == "backing-services"
            assert "affinity" not in crd["spec"]
        finally:
            app.config.pop("BACKING_SERVICES_POOL", None)

    def test_redis_cluster_crd(self, app, environment):
        from cabotage.celery.tasks.resources import _render_redis_cluster

        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Cluster",
            size_class="cache.xlarge",
            storage_size=10,
            ha_enabled=True,
        )
        db.session.add(r)
        db.session.flush()

        crd = _render_redis_cluster(r)
        assert crd["apiVersion"] == "redis.redis.opstreelabs.in/v1beta2"
        assert crd["kind"] == "RedisCluster"
        assert crd["spec"]["clusterSize"] == 3
        assert crd["spec"]["redisLeader"]["replicas"] == 3
        assert crd["spec"]["redisFollower"]["replicas"] == 3
        assert crd["spec"]["persistenceEnabled"] is True
        # TLS and password on cluster too
        assert crd["spec"]["TLS"]["secret"]["secretName"].endswith("-tls")
        assert "redisSecret" in crd["spec"]["kubernetesConfig"]
        assert "nodeSelector" not in crd["spec"]
        assert "tolerations" not in crd["spec"]

    def test_redis_cluster_crd_with_custom_replica_counts(self, app, environment):
        from cabotage.celery.tasks.resources import _render_redis_cluster

        r = RedisResource(
            service_version="8",
            environment_id=environment.id,
            name="Custom Cluster",
            size_class="cache.xlarge",
            storage_size=10,
            ha_enabled=True,
            leader_replicas=5,
            follower_replicas=2,
        )
        db.session.add(r)
        db.session.flush()

        crd = _render_redis_cluster(r)
        assert crd["spec"]["clusterSize"] == 5
        assert crd["spec"]["redisLeader"]["replicas"] == 5
        assert crd["spec"]["redisFollower"]["replicas"] == 2

    def test_redis_cluster_crd_adds_backing_services_pool_placement(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _render_redis_cluster

        app.config["BACKING_SERVICES_POOL"] = "backing-services"
        try:
            r = RedisResource(
                service_version="8",
                environment_id=environment.id,
                name="Placed Cluster",
                size_class="cache.xlarge",
                storage_size=10,
                ha_enabled=True,
                leader_replicas=3,
                follower_replicas=2,
            )
            db.session.add(r)
            db.session.flush()

            crd = _render_redis_cluster(r)
            assert crd["spec"]["nodeSelector"] == {
                "cabotage.dev/node-pool": "backing-services"
            }
            assert crd["spec"]["tolerations"][0]["value"] == "backing-services"
            assert (
                crd["spec"]["redisLeader"]["affinity"]["podAntiAffinity"][
                    "requiredDuringSchedulingIgnoredDuringExecution"
                ][0]["labelSelector"]["matchLabels"]["role"]
                == "leader"
            )
            assert (
                crd["spec"]["redisFollower"]["affinity"]["podAntiAffinity"][
                    "requiredDuringSchedulingIgnoredDuringExecution"
                ][0]["labelSelector"]["matchLabels"]["role"]
                == "follower"
            )
            assert (
                crd["spec"]["redisLeader"]["affinity"]["podAntiAffinity"][
                    "preferredDuringSchedulingIgnoredDuringExecution"
                ][0]["podAffinityTerm"]["labelSelector"]["matchLabels"][
                    "resident-pod.cabotage.io"
                ]
                == "true"
            )
            assert (
                crd["spec"]["redisLeader"]["affinity"]["podAntiAffinity"][
                    "preferredDuringSchedulingIgnoredDuringExecution"
                ][1]["podAffinityTerm"]["labelSelector"]["matchLabels"]["role"]
                == "leader"
            )
            assert (
                crd["spec"]["redisLeader"]["affinity"]["podAntiAffinity"][
                    "preferredDuringSchedulingIgnoredDuringExecution"
                ][1]["podAffinityTerm"]["topologyKey"]
                == "topology.kubernetes.io/zone"
            )
        finally:
            app.config.pop("BACKING_SERVICES_POOL", None)

    def test_redis_cluster_crd_avoids_required_anti_affinity_for_single_replica_roles(
        self, app, environment
    ):
        from cabotage.celery.tasks.resources import _render_redis_cluster

        app.config["BACKING_SERVICES_POOL"] = "backing-services"
        try:
            r = RedisResource(
                service_version="8",
                environment_id=environment.id,
                name="Light Cluster",
                size_class="cache.large",
                storage_size=5,
                ha_enabled=True,
                leader_replicas=1,
                follower_replicas=1,
            )
            db.session.add(r)
            db.session.flush()

            crd = _render_redis_cluster(r)
            leader_pod_anti_affinity = crd["spec"]["redisLeader"]["affinity"][
                "podAntiAffinity"
            ]
            follower_pod_anti_affinity = crd["spec"]["redisFollower"]["affinity"][
                "podAntiAffinity"
            ]
            assert (
                "requiredDuringSchedulingIgnoredDuringExecution"
                not in leader_pod_anti_affinity
            )
            assert (
                "requiredDuringSchedulingIgnoredDuringExecution"
                not in follower_pod_anti_affinity
            )
            assert (
                len(
                    leader_pod_anti_affinity[
                        "preferredDuringSchedulingIgnoredDuringExecution"
                    ]
                )
                == 1
            )
            assert (
                len(
                    follower_pod_anti_affinity[
                        "preferredDuringSchedulingIgnoredDuringExecution"
                    ]
                )
                == 1
            )
        finally:
            app.config.pop("BACKING_SERVICES_POOL", None)
