"""Tests for backing service resources (Postgres and Redis)."""

import datetime
import time
import uuid
from unittest.mock import MagicMock

import pytest
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
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


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestResourceModels:
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

    def test_create_redis_get_renders_form(
        self, client, admin_user, org, project, environment
    ):
        _login(client, admin_user)
        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/redis/create"
        )
        assert resp.status_code == 200
        assert b"Add Redis Instance" in resp.data

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
                "ha_enabled": "y",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

        db.session.refresh(r)
        assert r.size_class == "cache.xlarge"
        assert r.storage_size == 10
        assert r.ha_enabled is True

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
        mock_custom_api.get_namespaced_custom_object.side_effect = ApiException(
            status=404
        )

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
        return mock_custom_api, mock_core_api

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

        mock_custom_api, mock_core_api = self._mock_k8s_apis()
        _reconcile_postgres(r, mock_core_api, mock_custom_api)

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

    def test_reconcile_redis_standalone(self, app, environment):
        from cabotage.celery.tasks.resources import _reconcile_redis

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

        mock_custom_api, mock_core_api = self._mock_k8s_apis()
        _reconcile_redis(r, mock_core_api, mock_custom_api)

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
        assert r.connection_info["tls"] is True

    def test_reconcile_redis_cluster(self, app, environment):
        from cabotage.celery.tasks.resources import _reconcile_redis

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

        mock_custom_api, mock_core_api = self._mock_k8s_apis()
        _reconcile_redis(r, mock_core_api, mock_custom_api)

        create_calls = mock_custom_api.create_namespaced_custom_object.call_args_list
        cluster_call = create_calls[-1]
        assert cluster_call[0][3] == "redisclusters"
        body = cluster_call[0][4]
        assert body["kind"] == "RedisCluster"
        assert body["spec"]["clusterSize"] == 3

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
        mock_core_api.read_namespaced_secret.side_effect = Exception("K8s unreachable")

        with pytest.raises(Exception, match="K8s unreachable"):
            _reconcile_postgres(r, mock_core_api, mock_custom_api)

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
        _delete_postgres(r, mock_core_api, mock_custom_api)

        assert mock_custom_api.delete_namespaced_custom_object.call_count == 2
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
        _delete_redis(r, mock_core_api, mock_custom_api)

        assert mock_custom_api.delete_namespaced_custom_object.call_count == 3
        assert mock_core_api.delete_namespaced_secret.call_count == 2


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
        # backup not yet wired (requires object storage credentials)
        assert "backup" not in crd["spec"]

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
        # backup not yet wired (requires object storage credentials)
        assert "backup" not in crd["spec"]

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
        assert "backup" not in crd["spec"]

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
        assert crd["spec"]["persistenceEnabled"] is True
        # TLS and password on cluster too
        assert crd["spec"]["TLS"]["secret"]["secretName"].endswith("-tls")
        assert "redisSecret" in crd["spec"]["kubernetesConfig"]
