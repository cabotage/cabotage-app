"""Tests for image and release build tasks."""

import uuid
from unittest.mock import MagicMock, patch

import pytest

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Image,
    Project,
    Release,
    activity_plugin,
)
from cabotage.server.wsgi import app as _app

Activity = activity_plugin.activity_cls

REPO = "myorg/myrepo"
COMMIT_SHA = "abc123deadbeef" * 3  # 42 chars, truncated to 40 by git
BRANCH = "main"
REGISTRY = "registry.example.com"
REPOSITORY_NAME = "cabotage/testorg/testproj/webapp"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REGISTRY_AUTH_SECRET"] = "test-secret"
    _app.config["REGISTRY_BUILD"] = REGISTRY
    _app.config["REGISTRY_SECURE"] = False
    _app.config["REGISTRY_VERIFY"] = False
    _app.config["BUILDKIT_IMAGE"] = "moby/buildkit:latest"
    _app.config["KUBERNETES_ENABLED"] = True
    _app.config["CELERY_BROKER_URL"] = "redis://localhost:6379/0"
    _app.config["CABOTAGE_OMNIBUS_BUILDS"] = True
    with _app.app_context():
        yield _app


@pytest.fixture
def db_session(app):
    yield db.session
    db.session.rollback()


@pytest.fixture
def org(db_session):
    o = Organization(name="Test Org", slug=f"testorg-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    db_session.flush()
    return o


@pytest.fixture
def project(db_session, org):
    p = Project(name="Test Project", organization_id=org.id)
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture
def environment(db_session, project):
    e = Environment(name="default", project_id=project.id, ephemeral=False)
    db_session.add(e)
    db_session.flush()
    return e


@pytest.fixture
def application(db_session, project):
    a = Application(
        name="webapp",
        slug="webapp",
        project_id=project.id,
        github_app_installation_id=12345,
        github_repository=REPO,
        auto_deploy_branch=BRANCH,
    )
    db_session.add(a)
    db_session.flush()
    return a


@pytest.fixture
def app_env(db_session, application, environment):
    ae = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
    )
    db_session.add(ae)
    db_session.flush()
    return ae


@pytest.fixture
def image(db_session, application, app_env):
    img = Image(
        application_id=application.id,
        application_environment_id=app_env.id,
        _repository_name=REPOSITORY_NAME,
        image_metadata={
            "sha": COMMIT_SHA[:40],
            "installation_id": 12345,
            "auto_deploy": True,
        },
        build_ref=COMMIT_SHA[:40],
    )
    db_session.add(img)
    db_session.flush()
    return img


@pytest.fixture
def built_image(db_session, image):
    """An image that has been successfully built."""
    image.built = True
    image.build_job_id = "deadbeef"
    image.processes = {
        "web": {"cmd": "python -m http.server", "env": []},
    }
    image.image_id = "sha256:abc123"
    db_session.add(image)
    db_session.flush()
    return image


@pytest.fixture
def release(db_session, application, app_env, built_image):
    r = Release(
        application_id=application.id,
        application_environment_id=app_env.id,
        _repository_name=REPOSITORY_NAME,
        image=built_image.asdict,
        configuration={},
        image_changes={},
        configuration_changes={},
    )
    r.build_job_id = "cafebabe"
    db_session.add(r)
    db_session.flush()
    return r


# ---------------------------------------------------------------------------
# Helper: mock K8s and buildkit dependencies
# ---------------------------------------------------------------------------


def _mock_k8s():
    """Return a dict of mocks for the K8s API clients."""
    core = MagicMock()
    batch = MagicMock()
    # read PVC raises 404 so fetch_image_build_cache_volume_claim creates it
    from kubernetes.client.rest import ApiException

    core.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=404)
    pvc = MagicMock()
    pvc.metadata.name = "build-cache-pvc"
    core.create_namespaced_persistent_volume_claim.return_value = pvc
    return {"core": core, "batch": batch}


DOCKERFILE_BODY = 'FROM python:3.11\nENV APP_ENV production\nCMD ["python"]'
PROCFILE_BODY = "web: python -m http.server\nworker: celery -A app worker"


# ---------------------------------------------------------------------------
# Tests: build_image_buildkit
# ---------------------------------------------------------------------------


class TestBuildImageBuildkit:
    """Tests for the build_image_buildkit function."""

    @patch("cabotage.celery.tasks.build.run_job")
    @patch("cabotage.celery.tasks.build.fetch_image_build_cache_volume_claim")
    @patch("cabotage.celery.tasks.build.kubernetes_ext")
    @patch("cabotage.celery.tasks.build._fetch_github_file")
    @patch("cabotage.celery.tasks.build.GithubIntegration")
    @patch("cabotage.celery.tasks.build.GithubAppAuth")
    @patch("cabotage.celery.tasks.build.github_app")
    def test_successful_image_build(
        self,
        mock_github_app,
        mock_auth_cls,
        mock_gi_cls,
        mock_fetch_file,
        mock_k8s_ext,
        mock_fetch_pvc,
        mock_run_job,
        app,
        db_session,
        image,
    ):
        """A successful image build fetches source, creates K8s Job, returns metadata."""
        from cabotage.celery.tasks.build import build_image_buildkit

        # Mock GitHub auth
        mock_gi = MagicMock()
        mock_gi.get_access_token.return_value.token = "gh-token"
        mock_gi_cls.return_value = mock_gi

        # Mock file fetches: Dockerfile.cabotage not found, Dockerfile found
        mock_fetch_file.side_effect = [
            None,  # Dockerfile.cabotage
            DOCKERFILE_BODY,  # Dockerfile
            None,  # Procfile.cabotage
            PROCFILE_BODY,  # Procfile
        ]

        # Mock K8s
        mock_k8s_ext.kubernetes_client = MagicMock()
        pvc = MagicMock()
        pvc.metadata.name = "build-cache-pvc"
        mock_fetch_pvc.return_value = pvc

        mock_run_job.return_value = (True, "build logs here")

        # Mock DXF registry verification
        with patch("cabotage.celery.tasks.build.DXF") as mock_dxf_cls:
            mock_dxf = MagicMock()
            mock_dxf.get_digest.return_value = "sha256:image-digest-123"
            mock_dxf_cls.return_value = mock_dxf

            with patch(
                "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
            ):
                image.build_job_id = "test1234"
                result = build_image_buildkit(image)

        assert result["image_id"] == "sha256:image-digest-123"
        assert "web" in result["processes"]
        assert "worker" in result["processes"]
        assert result["dockerfile"] == DOCKERFILE_BODY
        assert result["procfile"] == PROCFILE_BODY
        assert isinstance(result["dockerfile_env_vars"], list)

        # Image record updated
        assert image.dockerfile == DOCKERFILE_BODY
        assert image.procfile == PROCFILE_BODY
        assert image.image_build_log == "build logs here"

    @patch("cabotage.celery.tasks.build.GithubIntegration")
    @patch("cabotage.celery.tasks.build.GithubAppAuth")
    @patch("cabotage.celery.tasks.build.github_app")
    @patch("cabotage.celery.tasks.build._fetch_github_file")
    def test_missing_dockerfile_raises(
        self,
        mock_fetch_file,
        mock_github_app,
        mock_auth_cls,
        mock_gi_cls,
        app,
        db_session,
        image,
    ):
        """BuildError raised when no Dockerfile found."""
        from cabotage.celery.tasks.build import build_image_buildkit, BuildError

        mock_gi = MagicMock()
        mock_gi.get_access_token.return_value.token = "gh-token"
        mock_gi_cls.return_value = mock_gi
        mock_fetch_file.return_value = None

        image.build_job_id = "test1234"
        with pytest.raises(BuildError, match="No Dockerfile"):
            build_image_buildkit(image)

    @patch("cabotage.celery.tasks.build.GithubIntegration")
    @patch("cabotage.celery.tasks.build.GithubAppAuth")
    @patch("cabotage.celery.tasks.build.github_app")
    @patch("cabotage.celery.tasks.build._fetch_github_file")
    def test_missing_procfile_raises(
        self,
        mock_fetch_file,
        mock_github_app,
        mock_auth_cls,
        mock_gi_cls,
        app,
        db_session,
        image,
    ):
        """BuildError raised when no Procfile found."""
        from cabotage.celery.tasks.build import build_image_buildkit, BuildError

        mock_gi = MagicMock()
        mock_gi.get_access_token.return_value.token = "gh-token"
        mock_gi_cls.return_value = mock_gi
        # Dockerfile found, but no Procfile
        mock_fetch_file.side_effect = [
            None,
            DOCKERFILE_BODY,  # Dockerfile.cabotage miss, Dockerfile hit
            None,
            None,  # Procfile.cabotage miss, Procfile miss
        ]

        image.build_job_id = "test1234"
        with pytest.raises(BuildError, match="No Procfile"):
            build_image_buildkit(image)

    @patch("cabotage.celery.tasks.build.GithubIntegration")
    @patch("cabotage.celery.tasks.build.GithubAppAuth")
    @patch("cabotage.celery.tasks.build.github_app")
    @patch("cabotage.celery.tasks.build._fetch_github_file")
    def test_whitespace_process_name_raises(
        self,
        mock_fetch_file,
        mock_github_app,
        mock_auth_cls,
        mock_gi_cls,
        app,
        db_session,
        image,
    ):
        """BuildError raised for process names with whitespace."""
        from cabotage.celery.tasks.build import build_image_buildkit, BuildError

        mock_gi = MagicMock()
        mock_gi.get_access_token.return_value.token = "gh-token"
        mock_gi_cls.return_value = mock_gi
        mock_fetch_file.side_effect = [
            None,
            "FROM python:3.11",
            None,
            "web server: python -m http.server",
        ]

        image.build_job_id = "test1234"
        with pytest.raises(BuildError, match="Invalid process name"):
            build_image_buildkit(image)

    @patch("cabotage.celery.tasks.build.run_job")
    @patch("cabotage.celery.tasks.build.fetch_image_build_cache_volume_claim")
    @patch("cabotage.celery.tasks.build.kubernetes_ext")
    @patch("cabotage.celery.tasks.build._fetch_github_file")
    @patch("cabotage.celery.tasks.build.GithubIntegration")
    @patch("cabotage.celery.tasks.build.GithubAppAuth")
    @patch("cabotage.celery.tasks.build.github_app")
    def test_failed_k8s_job_raises(
        self,
        mock_github_app,
        mock_auth_cls,
        mock_gi_cls,
        mock_fetch_file,
        mock_k8s_ext,
        mock_fetch_pvc,
        mock_run_job,
        app,
        db_session,
        image,
    ):
        """BuildError raised when the K8s Job fails."""
        from cabotage.celery.tasks.build import build_image_buildkit, BuildError

        mock_gi = MagicMock()
        mock_gi.get_access_token.return_value.token = "gh-token"
        mock_gi_cls.return_value = mock_gi
        mock_fetch_file.side_effect = [None, DOCKERFILE_BODY, None, PROCFILE_BODY]

        mock_k8s_ext.kubernetes_client = MagicMock()
        pvc = MagicMock()
        pvc.metadata.name = "build-cache-pvc"
        mock_fetch_pvc.return_value = pvc

        # Job fails
        mock_run_job.return_value = (False, "error: build failed")

        with patch(
            "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
        ):
            image.build_job_id = "test1234"
            with pytest.raises(BuildError, match="Image build failed"):
                build_image_buildkit(image)

        assert image.image_build_log == "error: build failed"


# ---------------------------------------------------------------------------
# Tests: build_release_buildkit
# ---------------------------------------------------------------------------


class TestBuildReleaseBuildkit:
    """Tests for the build_release_buildkit function."""

    @patch("cabotage.celery.tasks.build.run_job")
    @patch("cabotage.celery.tasks.build.fetch_image_build_cache_volume_claim")
    @patch("cabotage.celery.tasks.build.kubernetes_ext")
    def test_successful_release_build(
        self,
        mock_k8s_ext,
        mock_fetch_pvc,
        mock_run_job,
        app,
        db_session,
        release,
    ):
        """A successful release build generates Dockerfile and returns metadata."""
        from cabotage.celery.tasks.build import build_release_buildkit

        mock_k8s_ext.kubernetes_client = MagicMock()
        pvc = MagicMock()
        pvc.metadata.name = "build-cache-pvc"
        mock_fetch_pvc.return_value = pvc

        mock_run_job.return_value = (True, "release build logs")

        with patch("cabotage.celery.tasks.build.DXF") as mock_dxf_cls:
            mock_dxf = MagicMock()
            mock_dxf.get_digest.return_value = "sha256:release-digest-456"
            mock_dxf_cls.return_value = mock_dxf

            with patch(
                "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
            ):
                result = build_release_buildkit(release)

        assert result["release_id"] == "sha256:release-digest-456"
        assert release.release_build_log == "release build logs"

    @patch("cabotage.celery.tasks.build.run_job")
    @patch("cabotage.celery.tasks.build.fetch_image_build_cache_volume_claim")
    @patch("cabotage.celery.tasks.build.kubernetes_ext")
    def test_release_dockerfile_generated(
        self,
        mock_k8s_ext,
        mock_fetch_pvc,
        mock_run_job,
        app,
        db_session,
        release,
    ):
        """The release Dockerfile is generated from the template."""
        from cabotage.celery.tasks.build import build_release_buildkit

        mock_k8s_ext.kubernetes_client = MagicMock()
        pvc = MagicMock()
        pvc.metadata.name = "build-cache-pvc"
        mock_fetch_pvc.return_value = pvc

        mock_run_job.return_value = (True, "logs")

        with patch("cabotage.celery.tasks.build.DXF") as mock_dxf_cls:
            mock_dxf = MagicMock()
            mock_dxf.get_digest.return_value = "sha256:digest"
            mock_dxf_cls.return_value = mock_dxf

            with patch(
                "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
            ):
                build_release_buildkit(release)

        assert release.dockerfile is not None
        assert f"FROM {REGISTRY}/{REPOSITORY_NAME}" in release.dockerfile

    @patch("cabotage.celery.tasks.build.run_job")
    @patch("cabotage.celery.tasks.build.fetch_image_build_cache_volume_claim")
    @patch("cabotage.celery.tasks.build.kubernetes_ext")
    def test_failed_release_build_raises(
        self,
        mock_k8s_ext,
        mock_fetch_pvc,
        mock_run_job,
        app,
        db_session,
        release,
    ):
        """BuildError raised when the release K8s Job fails."""
        from cabotage.celery.tasks.build import build_release_buildkit, BuildError

        mock_k8s_ext.kubernetes_client = MagicMock()
        pvc = MagicMock()
        pvc.metadata.name = "build-cache-pvc"
        mock_fetch_pvc.return_value = pvc

        mock_run_job.return_value = (False, "release build error")

        with patch(
            "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
        ):
            with pytest.raises(BuildError):
                build_release_buildkit(release)


# ---------------------------------------------------------------------------
# Tests: run_image_build task
# ---------------------------------------------------------------------------


class TestRunImageBuild:
    """Tests for the run_image_build Celery task."""

    @patch("cabotage.celery.tasks.build.run_release_build")
    @patch("cabotage.celery.tasks.build.build_image_buildkit")
    @patch("cabotage.celery.tasks.build.github_app")
    def test_auto_deploy_creates_release_and_chains(
        self,
        mock_github_app,
        mock_build,
        mock_run_release,
        app,
        db_session,
        image,
    ):
        """When auto_deploy=True, run_image_build creates a Release and queues release build."""
        from cabotage.celery.tasks.build import run_image_build

        db_session.commit()
        mock_github_app.fetch_installation_access_token.return_value = "token"
        mock_github_app.slug = "cabotage"
        mock_build.return_value = {
            "image_id": "sha256:abc",
            "processes": {"web": {"cmd": "python", "env": []}},
            "dockerfile": DOCKERFILE_BODY,
            "procfile": PROCFILE_BODY,
            "dockerfile_env_vars": ["APP_ENV"],
        }

        with patch(
            "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
        ):
            with patch("cabotage.celery.tasks.build.CheckRun") as mock_check_cls:
                mock_check_cls.return_value = MagicMock()
                mock_check_cls.create.return_value = MagicMock(check_run_id=None)
                run_image_build(image_id=image.id)

        # Image should be marked as built
        db_session.refresh(image)
        assert image.built is True
        assert image.image_id == "sha256:abc"
        assert image.processes == {"web": {"cmd": "python", "env": []}}

        # A release should have been created
        releases = Release.query.filter_by(
            application_id=image.application_id,
            application_environment_id=image.application_environment_id,
        ).all()
        assert len(releases) == 1
        assert releases[0].image == image.asdict

        # release build should have been queued
        mock_run_release.delay.assert_called_once_with(release_id=releases[0].id)

    @patch("cabotage.celery.tasks.build.build_image_buildkit")
    @patch("cabotage.celery.tasks.build.github_app")
    def test_no_auto_deploy_skips_release(
        self,
        mock_github_app,
        mock_build,
        app,
        db_session,
        image,
    ):
        """When auto_deploy is not set, no Release is created."""
        from cabotage.celery.tasks.build import run_image_build

        # Remove auto_deploy from metadata
        image.image_metadata = {"sha": COMMIT_SHA[:40], "installation_id": 12345}
        db_session.add(image)
        db_session.commit()

        mock_github_app.fetch_installation_access_token.return_value = "token"
        mock_github_app.slug = "cabotage"
        mock_build.return_value = {
            "image_id": "sha256:abc",
            "processes": {"web": {"cmd": "python", "env": []}},
            "dockerfile": DOCKERFILE_BODY,
            "procfile": PROCFILE_BODY,
            "dockerfile_env_vars": [],
        }

        with patch(
            "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
        ):
            with patch("cabotage.celery.tasks.build.CheckRun") as mock_check_cls:
                mock_check_cls.return_value = MagicMock()
                mock_check_cls.create.return_value = MagicMock(check_run_id=None)
                run_image_build(image_id=image.id)

        releases = Release.query.filter_by(
            application_id=image.application_id,
            application_environment_id=image.application_environment_id,
        ).all()
        assert len(releases) == 0

    @patch("cabotage.celery.tasks.build.build_image_buildkit")
    @patch("cabotage.celery.tasks.build.github_app")
    def test_build_error_marks_image_as_error(
        self,
        mock_github_app,
        mock_build,
        app,
        db_session,
        image,
    ):
        """BuildError is recorded on the image record."""
        from cabotage.celery.tasks.build import run_image_build, BuildError

        db_session.commit()
        mock_github_app.fetch_installation_access_token.return_value = "token"
        mock_github_app.slug = "cabotage"
        mock_build.side_effect = BuildError("something broke")

        with patch(
            "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
        ):
            with patch("cabotage.celery.tasks.build.CheckRun") as mock_check_cls:
                mock_check_cls.return_value = MagicMock()
                mock_check_cls.create.return_value = MagicMock(check_run_id=None)
                with pytest.raises(BuildError):
                    run_image_build(image_id=image.id)

        db_session.refresh(image)
        assert image.error is True
        assert "something broke" in image.error_detail


# ---------------------------------------------------------------------------
# Tests: run_release_build task
# ---------------------------------------------------------------------------


class TestRunReleaseBuild:
    """Tests for the run_release_build Celery task."""

    @patch("cabotage.celery.tasks.build.run_deploy")
    @patch("cabotage.celery.tasks.build.build_release_buildkit")
    @patch("cabotage.celery.tasks.build.github_app")
    def test_auto_deploy_creates_deployment_and_chains(
        self,
        mock_github_app,
        mock_build,
        mock_run_deploy,
        app,
        db_session,
        release,
    ):
        """When auto_deploy=True, run_release_build creates Deployment and queues deploy."""
        from cabotage.celery.tasks.build import run_release_build

        release.release_metadata = {
            "auto_deploy": True,
            "sha": COMMIT_SHA[:40],
            "installation_id": 12345,
        }
        db_session.add(release)
        db_session.commit()

        mock_github_app.fetch_installation_access_token.return_value = "token"
        mock_build.return_value = {"release_id": "sha256:release-abc"}

        with patch(
            "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
        ):
            with patch("cabotage.celery.tasks.build.CheckRun") as mock_check_cls:
                mock_check = MagicMock()
                mock_check_cls.from_metadata.return_value = mock_check
                run_release_build(release_id=release.id)

        db_session.refresh(release)
        assert release.built is True
        assert release.release_id == "sha256:release-abc"

        from cabotage.server.models.projects import Deployment

        deployments = Deployment.query.filter_by(
            application_id=release.application_id,
            application_environment_id=release.application_environment_id,
        ).all()
        assert len(deployments) == 1
        mock_run_deploy.delay.assert_called_once()

    @patch("cabotage.celery.tasks.build.build_release_buildkit")
    @patch("cabotage.celery.tasks.build.github_app")
    def test_no_auto_deploy_skips_deployment(
        self,
        mock_github_app,
        mock_build,
        app,
        db_session,
        release,
    ):
        """When auto_deploy is not set, no Deployment is created."""
        from cabotage.celery.tasks.build import run_release_build

        release.release_metadata = {"sha": COMMIT_SHA[:40]}
        db_session.add(release)
        db_session.commit()

        mock_github_app.fetch_installation_access_token.return_value = "token"
        mock_build.return_value = {"release_id": "sha256:release-abc"}

        with patch(
            "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
        ):
            with patch("cabotage.celery.tasks.build.CheckRun") as mock_check_cls:
                mock_check_cls.from_metadata.return_value = MagicMock()
                run_release_build(release_id=release.id)

        from cabotage.server.models.projects import Deployment

        deployments = Deployment.query.filter_by(
            application_id=release.application_id,
        ).all()
        assert len(deployments) == 0

    @patch("cabotage.celery.tasks.build.build_release_buildkit")
    @patch("cabotage.celery.tasks.build.github_app")
    def test_build_error_marks_release_as_error(
        self,
        mock_github_app,
        mock_build,
        app,
        db_session,
        release,
    ):
        """BuildError is recorded on the release record."""
        from cabotage.celery.tasks.build import run_release_build, BuildError

        release.release_metadata = {"sha": COMMIT_SHA[:40]}
        db_session.add(release)
        db_session.commit()

        mock_github_app.fetch_installation_access_token.return_value = "token"
        mock_build.side_effect = BuildError("release broke")

        with patch(
            "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
        ):
            with patch("cabotage.celery.tasks.build.CheckRun") as mock_check_cls:
                mock_check_cls.from_metadata.return_value = MagicMock()
                # BuildError is caught internally by run_release_build
                # (it records the error but doesn't re-raise)
                run_release_build(release_id=release.id)

        db_session.refresh(release)
        assert release.error is True
        assert "release broke" in release.error_detail


# ---------------------------------------------------------------------------
# Tests: build_omnibus_buildkit
# ---------------------------------------------------------------------------


class TestBuildOmnibusBuildkit:
    """Tests for the build_omnibus_buildkit function."""

    @patch("cabotage.celery.tasks.build.run_job")
    @patch("cabotage.celery.tasks.build.fetch_image_build_cache_volume_claim")
    @patch("cabotage.celery.tasks.build.kubernetes_ext")
    @patch("cabotage.celery.tasks.build._fetch_image_source")
    @patch("cabotage.celery.tasks.build._fetch_github_access_token")
    def test_successful_omnibus_build(
        self,
        mock_fetch_token,
        mock_fetch_source,
        mock_k8s_ext,
        mock_fetch_pvc,
        mock_run_job,
        app,
        db_session,
        image,
        release,
    ):
        """Omnibus build returns both image and release digests."""
        from cabotage.celery.tasks.build import build_omnibus_buildkit

        mock_fetch_token.return_value = "gh-token"
        mock_fetch_source.return_value = {
            "git_ref": lambda repo, sha: f"https://github.com/{repo}#{sha}",
            "dockerfile_body": DOCKERFILE_BODY,
            "dockerfile_name": "Dockerfile",
            "procfile_body": PROCFILE_BODY,
            "processes": {"web": {"cmd": "python -m http.server", "env": []}},
            "dockerfile_env_vars": ["APP_ENV"],
        }

        mock_k8s_ext.kubernetes_client = MagicMock()
        pvc = MagicMock()
        pvc.metadata.name = "build-cache-pvc"
        mock_fetch_pvc.return_value = pvc
        mock_run_job.return_value = (True, "omnibus build logs")

        with patch("cabotage.celery.tasks.build.DXF") as mock_dxf_cls:
            mock_dxf = MagicMock()
            mock_dxf.get_digest.side_effect = [
                "sha256:image-digest",
                "sha256:release-digest",
            ]
            mock_dxf_cls.return_value = mock_dxf

            with patch(
                "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
            ):
                image.build_job_id = "omni1234"
                result = build_omnibus_buildkit(image, release)

        assert result["image_id"] == "sha256:image-digest"
        assert result["release_id"] == "sha256:release-digest"
        assert result["processes"] == {
            "web": {"cmd": "python -m http.server", "env": []}
        }

        # Image should be marked built with processes populated
        assert image.built is True
        assert image.processes == {"web": {"cmd": "python -m http.server", "env": []}}
        assert image.image_build_log == "omnibus build logs"

        # Release should have image snapshot and build log
        assert release.image == image.asdict
        assert release.release_build_log == "omnibus build logs"
        assert release.dockerfile is not None
        assert f"FROM {REGISTRY}/{REPOSITORY_NAME}" in release.dockerfile

    @patch("cabotage.celery.tasks.build.run_job")
    @patch("cabotage.celery.tasks.build.fetch_image_build_cache_volume_claim")
    @patch("cabotage.celery.tasks.build.kubernetes_ext")
    @patch("cabotage.celery.tasks.build._fetch_image_source")
    @patch("cabotage.celery.tasks.build._fetch_github_access_token")
    def test_failed_job_raises(
        self,
        mock_fetch_token,
        mock_fetch_source,
        mock_k8s_ext,
        mock_fetch_pvc,
        mock_run_job,
        app,
        db_session,
        image,
        release,
    ):
        """BuildError raised when the K8s Job fails."""
        from cabotage.celery.tasks.build import build_omnibus_buildkit, BuildError

        mock_fetch_token.return_value = "gh-token"
        mock_fetch_source.return_value = {
            "git_ref": lambda repo, sha: f"https://github.com/{repo}#{sha}",
            "dockerfile_body": DOCKERFILE_BODY,
            "dockerfile_name": "Dockerfile",
            "procfile_body": PROCFILE_BODY,
            "processes": {"web": {"cmd": "python", "env": []}},
            "dockerfile_env_vars": [],
        }

        mock_k8s_ext.kubernetes_client = MagicMock()
        pvc = MagicMock()
        pvc.metadata.name = "build-cache-pvc"
        mock_fetch_pvc.return_value = pvc
        mock_run_job.return_value = (False, "job failed")

        with patch(
            "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
        ):
            image.build_job_id = "omni1234"
            with pytest.raises(BuildError, match="Omnibus build failed"):
                build_omnibus_buildkit(image, release)

    @patch("cabotage.celery.tasks.build._fetch_image_source")
    @patch("cabotage.celery.tasks.build._fetch_github_access_token")
    def test_requires_kubernetes_enabled(
        self,
        mock_fetch_token,
        mock_fetch_source,
        app,
        db_session,
        image,
        release,
    ):
        """BuildError raised when KUBERNETES_ENABLED is False."""
        from cabotage.celery.tasks.build import build_omnibus_buildkit, BuildError

        app.config["KUBERNETES_ENABLED"] = False
        mock_fetch_token.return_value = "gh-token"
        mock_fetch_source.return_value = {
            "git_ref": lambda repo, sha: f"https://github.com/{repo}#{sha}",
            "dockerfile_body": DOCKERFILE_BODY,
            "dockerfile_name": "Dockerfile",
            "procfile_body": PROCFILE_BODY,
            "processes": {"web": {"cmd": "python", "env": []}},
            "dockerfile_env_vars": [],
        }

        image.build_job_id = "omni1234"
        with pytest.raises(BuildError, match="requires KUBERNETES_ENABLED"):
            build_omnibus_buildkit(image, release)

    @patch("cabotage.celery.tasks.build.run_job")
    @patch("cabotage.celery.tasks.build.fetch_image_build_cache_volume_claim")
    @patch("cabotage.celery.tasks.build.kubernetes_ext")
    @patch("cabotage.celery.tasks.build._fetch_image_source")
    @patch("cabotage.celery.tasks.build._fetch_github_access_token")
    def test_single_run_job_call(
        self,
        mock_fetch_token,
        mock_fetch_source,
        mock_k8s_ext,
        mock_fetch_pvc,
        mock_run_job,
        app,
        db_session,
        image,
        release,
    ):
        """Omnibus build calls run_job exactly once (not twice)."""
        from cabotage.celery.tasks.build import build_omnibus_buildkit

        mock_fetch_token.return_value = "gh-token"
        mock_fetch_source.return_value = {
            "git_ref": lambda repo, sha: f"https://github.com/{repo}#{sha}",
            "dockerfile_body": DOCKERFILE_BODY,
            "dockerfile_name": "Dockerfile",
            "procfile_body": PROCFILE_BODY,
            "processes": {"web": {"cmd": "python", "env": []}},
            "dockerfile_env_vars": [],
        }

        mock_k8s_ext.kubernetes_client = MagicMock()
        pvc = MagicMock()
        pvc.metadata.name = "build-cache-pvc"
        mock_fetch_pvc.return_value = pvc
        mock_run_job.return_value = (True, "logs")

        with patch("cabotage.celery.tasks.build.DXF") as mock_dxf_cls:
            mock_dxf = MagicMock()
            mock_dxf.get_digest.return_value = "sha256:digest"
            mock_dxf_cls.return_value = mock_dxf

            with patch(
                "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
            ):
                image.build_job_id = "omni1234"
                build_omnibus_buildkit(image, release)

        # The whole point: one Job, one run_job call
        assert mock_run_job.call_count == 1


# ---------------------------------------------------------------------------
# Tests: run_omnibus_build task
# ---------------------------------------------------------------------------


class TestRunOmnibusBuild:
    """Tests for the run_omnibus_build Celery task."""

    @patch("cabotage.celery.tasks.build.run_deploy")
    @patch("cabotage.celery.tasks.build.build_omnibus_buildkit")
    @patch("cabotage.celery.tasks.build.github_app")
    def test_creates_release_and_deployment(
        self,
        mock_github_app,
        mock_build,
        mock_run_deploy,
        app,
        db_session,
        image,
    ):
        """run_omnibus_build creates Release, Deployment and queues deploy."""
        from cabotage.celery.tasks.build import run_omnibus_build

        db_session.commit()
        mock_github_app.fetch_installation_access_token.return_value = "token"
        mock_github_app.slug = "cabotage"
        mock_build.return_value = {
            "image_id": "sha256:img",
            "release_id": "sha256:rel",
            "processes": {"web": {"cmd": "python", "env": []}},
            "dockerfile": DOCKERFILE_BODY,
            "procfile": PROCFILE_BODY,
            "dockerfile_env_vars": [],
        }

        with patch(
            "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
        ):
            with patch("cabotage.celery.tasks.build.CheckRun") as mock_check_cls:
                mock_check_cls.return_value = MagicMock()
                mock_check_cls.create.return_value = MagicMock(check_run_id=None)
                run_omnibus_build(image_id=image.id)

        # Image updated
        db_session.refresh(image)
        assert image.image_id == "sha256:img"

        # Release created and marked built
        releases = Release.query.filter_by(
            application_id=image.application_id,
            application_environment_id=image.application_environment_id,
        ).all()
        assert len(releases) == 1
        assert releases[0].built is True
        assert releases[0].release_id == "sha256:rel"
        assert releases[0].image == image.asdict

        # Deployment created
        from cabotage.server.models.projects import Deployment

        deployments = Deployment.query.filter_by(
            application_id=image.application_id,
        ).all()
        assert len(deployments) == 1
        mock_run_deploy.delay.assert_called_once()

    @patch("cabotage.celery.tasks.build.build_omnibus_buildkit")
    @patch("cabotage.celery.tasks.build.github_app")
    def test_build_error_marks_both_records(
        self,
        mock_github_app,
        mock_build,
        app,
        db_session,
        image,
    ):
        """BuildError marks both image and release as errored."""
        from cabotage.celery.tasks.build import run_omnibus_build, BuildError

        db_session.commit()
        mock_github_app.fetch_installation_access_token.return_value = "token"
        mock_github_app.slug = "cabotage"
        mock_build.side_effect = BuildError("omnibus broke")

        with patch(
            "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
        ):
            with patch("cabotage.celery.tasks.build.CheckRun") as mock_check_cls:
                mock_check_cls.return_value = MagicMock()
                mock_check_cls.create.return_value = MagicMock(check_run_id=None)
                with pytest.raises(BuildError):
                    run_omnibus_build(image_id=image.id)

        db_session.refresh(image)
        assert image.error is True
        assert "omnibus broke" in image.error_detail

        # Release should also be marked as error
        releases = Release.query.filter_by(
            application_id=image.application_id,
            application_environment_id=image.application_environment_id,
        ).all()
        assert len(releases) == 1
        assert releases[0].error is True

    @patch("cabotage.celery.tasks.build.build_omnibus_buildkit")
    @patch("cabotage.celery.tasks.build.github_app")
    def test_no_separate_release_build_queued(
        self,
        mock_github_app,
        mock_build,
        app,
        db_session,
        image,
    ):
        """Omnibus build does NOT queue a separate run_release_build."""
        from cabotage.celery.tasks.build import run_omnibus_build

        db_session.commit()
        mock_github_app.fetch_installation_access_token.return_value = "token"
        mock_github_app.slug = "cabotage"
        mock_build.return_value = {
            "image_id": "sha256:img",
            "release_id": "sha256:rel",
            "processes": {"web": {"cmd": "python", "env": []}},
            "dockerfile": DOCKERFILE_BODY,
            "procfile": PROCFILE_BODY,
            "dockerfile_env_vars": [],
        }

        with patch(
            "cabotage.celery.tasks.build.get_redis_client", side_effect=Exception
        ):
            with patch("cabotage.celery.tasks.build.CheckRun") as mock_check_cls:
                mock_check_cls.return_value = MagicMock()
                mock_check_cls.create.return_value = MagicMock(check_run_id=None)
                with patch(
                    "cabotage.celery.tasks.build.run_release_build"
                ) as mock_release:
                    with patch("cabotage.celery.tasks.build.run_deploy"):
                        run_omnibus_build(image_id=image.id)

        mock_release.delay.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: git_ref fallback for manual builds without commit SHA
# ---------------------------------------------------------------------------


class TestGitRefFallback:
    """Tests that builds without a resolved commit SHA use build_ref instead of #null."""

    @pytest.fixture
    def no_github_app(self, db_session, application):
        """Remove github_app_installation_id so builds don't try to authenticate."""
        application.github_app_installation_id = None
        db_session.flush()
        return application

    @pytest.fixture
    def manual_image(self, db_session, no_github_app, app_env):
        """An image created by a manual build — no SHA, just a branch ref."""
        img = Image(
            application_id=no_github_app.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image_metadata={
                "trigger": "manual_build",
                "triggered_by": "testuser",
            },
            build_ref="develop",
        )
        db_session.add(img)
        db_session.flush()
        return img

    @patch("cabotage.celery.tasks.build.run_job")
    @patch("cabotage.celery.tasks.build.fetch_image_build_cache_volume_claim")
    @patch("cabotage.celery.tasks.build.kubernetes_ext")
    @patch("cabotage.celery.tasks.build._fetch_github_file")
    @patch("cabotage.celery.tasks.build._fetch_commit_sha_for_ref")
    def test_manual_build_uses_branch_ref_when_sha_unresolved(
        self,
        mock_fetch_sha,
        mock_fetch_file,
        mock_k8s_ext,
        mock_fetch_pvc,
        mock_run_job,
        app,
        db_session,
        manual_image,
    ):
        """When _fetch_commit_sha_for_ref returns None, buildctl uses build_ref."""
        from cabotage.celery.tasks.build import build_image_buildkit

        # SHA resolution fails (returns None)
        mock_fetch_sha.return_value = None

        mock_fetch_file.side_effect = [
            None,  # Dockerfile.cabotage
            DOCKERFILE_BODY,  # Dockerfile
            None,  # Procfile.cabotage
            PROCFILE_BODY,  # Procfile
        ]

        mock_k8s_ext.kubernetes_client = MagicMock()
        pvc = MagicMock()
        pvc.metadata.name = "build-cache-pvc"
        mock_fetch_pvc.return_value = pvc
        mock_run_job.return_value = (True, "build logs")

        with patch("cabotage.celery.tasks.build.DXF") as mock_dxf_cls:
            mock_dxf = MagicMock()
            mock_dxf.get_digest.return_value = "sha256:manual-digest"
            mock_dxf_cls.return_value = mock_dxf

            with patch(
                "cabotage.celery.tasks.build.get_redis_client",
                side_effect=Exception,
            ):
                manual_image.build_job_id = "manual1234"
                build_image_buildkit(manual_image)

        # Check the buildctl args passed to run_job — job_object is 4th positional arg
        run_job_call = mock_run_job.call_args
        job_object = run_job_call[0][3]
        containers = job_object.spec.template.spec.containers
        args = containers[0].args
        context_arg = [a for a in args if "context=" in a][0]

        # Should use branch ref "develop", NOT "#null"
        assert "#null" not in context_arg, (
            f"Build context should not contain #null, got: {context_arg}"
        )
        assert "#develop" in context_arg, (
            f"Build context should use branch ref 'develop', got: {context_arg}"
        )

    @patch("cabotage.celery.tasks.build._fetch_github_file")
    @patch("cabotage.celery.tasks.build._fetch_commit_sha_for_ref")
    def test_fetch_image_source_resolves_sha_into_metadata(
        self,
        mock_fetch_sha,
        mock_fetch_file,
        app,
        db_session,
        manual_image,
    ):
        """When SHA resolution succeeds, the SHA is stored in image_metadata."""
        from cabotage.celery.tasks.build import _fetch_image_source

        resolved_sha = "abc123def456abc123def456abc123def456abc1"
        mock_fetch_sha.return_value = resolved_sha

        mock_fetch_file.side_effect = [
            None,
            DOCKERFILE_BODY,
            None,
            PROCFILE_BODY,
        ]

        _fetch_image_source(manual_image, access_token=None)

        assert manual_image.image_metadata["sha"] == resolved_sha
        assert manual_image.commit_sha == resolved_sha

    @patch("cabotage.celery.tasks.build._fetch_github_file")
    @patch("cabotage.celery.tasks.build._fetch_commit_sha_for_ref")
    def test_fetch_commit_sha_called_with_build_ref(
        self,
        mock_fetch_sha,
        mock_fetch_file,
        app,
        db_session,
        manual_image,
    ):
        """Manual image with no SHA triggers _fetch_commit_sha_for_ref with build_ref."""
        from cabotage.celery.tasks.build import _fetch_image_source

        mock_fetch_sha.return_value = None

        mock_fetch_file.side_effect = [
            None,
            DOCKERFILE_BODY,
            None,
            PROCFILE_BODY,
        ]

        _fetch_image_source(manual_image, access_token=None)

        mock_fetch_sha.assert_called_once_with(
            REPO, "develop", access_token=None
        )

    def test_manual_image_commit_sha_is_null(self, manual_image):
        """A manual image without a SHA in metadata reports commit_sha as 'null'."""
        assert manual_image.commit_sha == "null"
        assert manual_image.build_ref == "develop"
