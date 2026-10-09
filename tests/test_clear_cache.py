"""Tests for application cache-clearing behavior."""

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Image,
    Project,
)
from cabotage.server.wsgi import app as _app


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REQUIRE_MFA"] = False
    _app.config["KUBERNETES_ENABLED"] = True
    _app.config["KUBERNETES_BUILD_NAMESPACE"] = "tenant-builds-custom"
    _app.config["BUILDKIT_IMAGE"] = "moby/buildkit:latest"
    _app.config["REGISTRY_BUILD"] = "registry.example.com"
    _app.config["REGISTRY_SECURE"] = False
    _app.config["REGISTRY_VERIFY"] = False
    with _app.app_context():
        yield _app
    _app.config["REQUIRE_MFA"] = True
    _app.config["KUBERNETES_ENABLED"] = False
    _app.config["KUBERNETES_BUILD_NAMESPACE"] = "cabotage-tenant-builds"


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db_session(app):
    yield db.session
    db.session.rollback()


@pytest.fixture
def admin_user(db_session):
    user = User(
        username=f"admin-{uuid.uuid4().hex[:8]}",
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def org(db_session, admin_user):
    org = Organization(name="Test Org", slug=f"testorg-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.flush()
    db_session.add(
        OrganizationMember(organization_id=org.id, user_id=admin_user.id, admin=True)
    )
    db_session.flush()
    return org


@pytest.fixture
def project(db_session, org):
    project = Project(name="Test Project", organization_id=org.id)
    db_session.add(project)
    db_session.flush()
    return project


@pytest.fixture
def environment(db_session, project):
    environment = Environment(name="default", project_id=project.id, ephemeral=False)
    db_session.add(environment)
    db_session.flush()
    return environment


@pytest.fixture
def application(db_session, project):
    application = Application(
        name="webapp",
        slug="webapp",
        project_id=project.id,
        github_repository="myorg/myrepo",
    )
    db_session.add(application)
    db_session.flush()
    return application


@pytest.fixture
def app_env(db_session, application, environment):
    app_env = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
    )
    db_session.add(app_env)
    db_session.flush()
    return app_env


@pytest.fixture
def image(db_session, application, app_env):
    image = Image(
        application_id=application.id,
        application_environment_id=app_env.id,
        _repository_name=application.registry_repository_name(app_env),
        build_ref="main",
    )
    db_session.add(image)
    db_session.flush()
    return image


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"


class TestApplicationClearCache:
    def test_clear_cache_runs_job_in_configured_build_namespace(
        self, client, admin_user, org, project, application, app_env, image
    ):
        _login(client, admin_user)

        volume_claim = MagicMock()
        volume_claim.metadata.name = "build-cache-pvc"
        dxf_client = MagicMock()

        with (
            patch("cabotage.server.user.views.kubernetes_ext") as mock_kext,
            patch(
                "cabotage.server.user.views.kubernetes.client.CoreV1Api",
                return_value=MagicMock(),
            ),
            patch(
                "cabotage.server.user.views.kubernetes.client.BatchV1Api",
                return_value=MagicMock(),
            ),
            patch(
                "cabotage.celery.tasks.build.fetch_image_build_cache_volume_claim",
                return_value=volume_claim,
            ),
            patch(
                "cabotage.celery.tasks.deploy.run_job",
                return_value=(True, "cleared"),
            ) as mock_run_job,
            patch("cabotage.server.user.views.DXF", return_value=dxf_client),
        ):
            mock_kext.kubernetes_client = MagicMock()
            dxf_client.get_alias.side_effect = [
                MagicMock(),
                MagicMock(),
            ]

            response = client.post(
                (
                    f"/projects/{org.slug}/{project.slug}/applications/"
                    f"{application.slug}/clearcache"
                )
            )

        assert response.status_code == 302
        assert mock_run_job.call_args[0][2] == "tenant-builds-custom"
