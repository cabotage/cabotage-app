"""Tests for build job namespace and label behavior.

Verifies that image/release/omnibus builds:
- Run in the tenant namespace (not 'default')
- Use the build-job.cabotage.io label (not resident-job)
- Clean up resources in the correct namespace
"""

from unittest.mock import MagicMock, patch

import pytest

import cabotage.celery.tasks.build as build_module
from cabotage.celery.tasks.build import _build_namespace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BUILD_MODULE = "cabotage.celery.tasks.build"


def _make_app_env(org_k8s="test-org", env_k8s="production", env_enabled=True):
    app_env = MagicMock()
    app_env.application.project.organization.k8s_identifier = org_k8s
    if env_enabled:
        app_env.k8s_identifier = env_k8s
        app_env.environment.k8s_identifier = env_k8s
    else:
        app_env.k8s_identifier = None
    return app_env


def _make_release(org_k8s="test-org", env_k8s="production", env_enabled=True):
    release = MagicMock()
    release.application.project.organization.slug = "test-org"
    release.application.project.organization.k8s_identifier = org_k8s
    release.application.project.slug = "test-project"
    release.application.slug = "test-app"
    release.version = 1
    release.build_job_id = "abc123"
    release.repository_name = "test-org/test-app"
    release.envconsul_configurations = {}

    app_env = _make_app_env(org_k8s, env_k8s, env_enabled)
    app_env.application = release.application
    release.application_environment = app_env

    return release


def _make_image(org_k8s="test-org", env_k8s="production", env_enabled=True):
    image = MagicMock()
    image.application.project.organization.slug = "test-org"
    image.application.project.organization.k8s_identifier = org_k8s
    image.application.project.slug = "test-project"
    image.application.slug = "test-app"
    image.application.github_repository = "test-org/test-repo"
    image.application.github_repository_is_private = False
    image.application.github_app_installation_id = 12345
    image.build_job_id = "def456"
    image.repository_name = "test-org/test-app"
    image.commit_sha = "deadbeef"

    app_env = _make_app_env(org_k8s, env_k8s, env_enabled)
    app_env.application = image.application
    image.application_environment = app_env
    image.application_environment_id = "some-id"

    return image


@pytest.fixture()
def mock_app():
    mock = MagicMock()
    mock.config = {
        "KUBERNETES_ENABLED": True,
        "SIDECAR_IMAGE": "ghcr.io/cabotage/containers/sidecar-rs:1.0",
    }
    with patch.object(build_module, "current_app", mock):
        yield mock


def _run_release_build(release, mock_core, mock_run_job):
    """Run build_release_buildkit with all non-k8s dependencies mocked."""
    mock_batch = MagicMock()
    mock_bke = MagicMock()
    mock_bke.registry = "registry.example.com"
    mock_bke.buildkit_image = "moby/buildkit:latest"
    mock_bke.insecure_reg = ""
    mock_bke.dockerconfigjson = "{}"
    mock_bke.buildkitd_toml = ""
    mock_bke.tls_context_args.return_value = []

    with (
        patch(f"{_BUILD_MODULE}.kubernetes_ext") as mock_kext,
        patch(f"{_BUILD_MODULE}.kubernetes.client.CoreV1Api", return_value=mock_core),
        patch(f"{_BUILD_MODULE}.kubernetes.client.BatchV1Api", return_value=mock_batch),
        patch(f"{_BUILD_MODULE}.run_job", mock_run_job),
        patch(f"{_BUILD_MODULE}.BuildkitEnv", return_value=mock_bke),
        patch(f"{_BUILD_MODULE}.fetch_image_build_cache_volume_claim"),
        patch(f"{_BUILD_MODULE}.db"),
    ):
        mock_kext.kubernetes_client = MagicMock()
        build_module.build_release_buildkit(release)


def _run_image_build(image, mock_core, mock_run_job):
    """Run build_image_buildkit with all non-k8s dependencies mocked."""
    mock_batch = MagicMock()
    mock_bke = MagicMock()
    mock_bke.registry = "registry.example.com"
    mock_bke.buildkit_image = "moby/buildkit:latest"
    mock_bke.insecure_reg = ""
    mock_bke.dockerconfigjson = "{}"
    mock_bke.buildkitd_toml = ""
    mock_bke.tls_context_args.return_value = []

    with (
        patch(f"{_BUILD_MODULE}.kubernetes_ext") as mock_kext,
        patch(f"{_BUILD_MODULE}.kubernetes.client.CoreV1Api", return_value=mock_core),
        patch(f"{_BUILD_MODULE}.kubernetes.client.BatchV1Api", return_value=mock_batch),
        patch(f"{_BUILD_MODULE}.run_job", mock_run_job),
        patch(f"{_BUILD_MODULE}.BuildkitEnv", return_value=mock_bke),
        patch(f"{_BUILD_MODULE}.fetch_image_build_cache_volume_claim"),
        patch(f"{_BUILD_MODULE}._fetch_github_access_token", return_value="tok"),
        patch(
            f"{_BUILD_MODULE}._fetch_image_source",
            return_value={
                "git_ref": lambda r, s: f"https://example.com/{r}#{s}",
                "dockerfile_name": "Dockerfile",
                "dockerfile_body": "FROM scratch",
                "procfile_body": "web: start",
                "processes": {"web": "start"},
                "dockerfile_env_vars": {},
            },
        ),
        patch(f"{_BUILD_MODULE}.db"),
    ):
        mock_kext.kubernetes_client = MagicMock()
        build_module.build_image_buildkit(image=image)


# ---------------------------------------------------------------------------
# _build_namespace
# ---------------------------------------------------------------------------


class TestBuildNamespace:
    def test_always_returns_tenant_builds_namespace(self):
        app_env = _make_app_env(org_k8s="myorg", env_k8s="staging", env_enabled=True)
        assert _build_namespace(app_env) == "cabotage-tenant-builds"

    def test_env_disabled_still_returns_tenant_builds(self):
        app_env = _make_app_env(org_k8s="myorg", env_enabled=False)
        assert _build_namespace(app_env) == "cabotage-tenant-builds"


# ---------------------------------------------------------------------------
# Build job labels
# ---------------------------------------------------------------------------


class TestBuildJobLabels:
    def test_release_build_uses_build_job_label(self, mock_app):
        release = _make_release()
        mock_core = MagicMock()
        mock_run_job = MagicMock(return_value=(True, "logs"))

        _run_release_build(release, mock_core, mock_run_job)

        job_object = mock_run_job.call_args[0][3]
        labels = job_object.metadata.labels
        assert labels["build-job.cabotage.io"] == "true"
        assert "resident-job.cabotage.io" not in labels

    def test_image_build_uses_build_job_label(self, mock_app):
        image = _make_image()
        mock_core = MagicMock()
        mock_run_job = MagicMock(return_value=(True, "logs"))

        _run_image_build(image, mock_core, mock_run_job)

        job_object = mock_run_job.call_args[0][3]
        labels = job_object.metadata.labels
        assert labels["build-job.cabotage.io"] == "true"
        assert "resident-job.cabotage.io" not in labels


# ---------------------------------------------------------------------------
# Build job namespace
# ---------------------------------------------------------------------------


class TestBuildJobNamespace:
    def test_release_build_runs_in_tenant_namespace(self, mock_app):
        release = _make_release(org_k8s="myorg", env_k8s="prod")
        mock_core = MagicMock()
        mock_run_job = MagicMock(return_value=(True, "logs"))

        _run_release_build(release, mock_core, mock_run_job)

        ns_arg = mock_run_job.call_args[0][2]
        assert ns_arg == "cabotage-tenant-builds"

    def test_release_build_creates_resources_in_tenant_namespace(self, mock_app):
        release = _make_release(org_k8s="myorg", env_k8s="prod")
        mock_core = MagicMock()
        mock_run_job = MagicMock(return_value=(True, "logs"))

        _run_release_build(release, mock_core, mock_run_job)

        for c in mock_core.create_namespaced_config_map.call_args_list:
            assert (
                c[0][0] == "cabotage-tenant-builds"
            ), f"configmap created in wrong ns: {c}"
        for c in mock_core.create_namespaced_secret.call_args_list:
            assert (
                c[0][0] == "cabotage-tenant-builds"
            ), f"secret created in wrong ns: {c}"

    def test_release_build_cleans_up_in_tenant_namespace(self, mock_app):
        release = _make_release(org_k8s="myorg", env_k8s="prod")
        mock_core = MagicMock()
        mock_run_job = MagicMock(return_value=(True, "logs"))

        _run_release_build(release, mock_core, mock_run_job)

        for c in mock_core.delete_namespaced_secret.call_args_list:
            assert (
                c[0][1] == "cabotage-tenant-builds"
            ), f"secret deleted in wrong ns: {c}"
        for c in mock_core.delete_namespaced_config_map.call_args_list:
            assert (
                c[0][1] == "cabotage-tenant-builds"
            ), f"configmap deleted in wrong ns: {c}"

    def test_image_build_runs_in_tenant_namespace(self, mock_app):
        image = _make_image(org_k8s="myorg", env_k8s="staging")
        mock_core = MagicMock()
        mock_run_job = MagicMock(return_value=(True, "logs"))

        _run_image_build(image, mock_core, mock_run_job)

        ns_arg = mock_run_job.call_args[0][2]
        assert ns_arg == "cabotage-tenant-builds"

    def test_image_build_cleans_up_in_tenant_namespace(self, mock_app):
        image = _make_image(org_k8s="myorg", env_k8s="staging")
        mock_core = MagicMock()
        mock_run_job = MagicMock(return_value=(True, "logs"))

        _run_image_build(image, mock_core, mock_run_job)

        for c in mock_core.delete_namespaced_secret.call_args_list:
            assert (
                c[0][1] == "cabotage-tenant-builds"
            ), f"secret deleted in wrong ns: {c}"
        for c in mock_core.delete_namespaced_config_map.call_args_list:
            assert (
                c[0][1] == "cabotage-tenant-builds"
            ), f"configmap deleted in wrong ns: {c}"

    def test_legacy_app_uses_tenant_builds_namespace(self, mock_app):
        release = _make_release(org_k8s="myorg", env_enabled=False)
        mock_core = MagicMock()
        mock_run_job = MagicMock(return_value=(True, "logs"))

        _run_release_build(release, mock_core, mock_run_job)

        ns_arg = mock_run_job.call_args[0][2]
        assert ns_arg == "cabotage-tenant-builds"


# ---------------------------------------------------------------------------
# Reaper ignores build jobs
# ---------------------------------------------------------------------------


class TestReaperIgnoresBuildJobs:
    def test_reaper_label_selector_excludes_build_jobs(self):
        from cabotage.celery.tasks import reap_jobs

        mock_batch = MagicMock()
        mock_batch.list_job_for_all_namespaces.return_value = MagicMock(items=[])

        mock_app = MagicMock()
        mock_app.config = {"KUBERNETES_ENABLED": True}

        with (
            patch.object(reap_jobs, "current_app", mock_app),
            patch.object(reap_jobs, "kubernetes_ext") as mock_kext,
            patch("kubernetes.client.BatchV1Api", return_value=mock_batch),
        ):
            mock_kext.kubernetes_client = MagicMock()
            reap_jobs.reap_finished_jobs()

        selector = mock_batch.list_job_for_all_namespaces.call_args[1]["label_selector"]
        assert selector == "resident-job.cabotage.io=true"
        assert "build-job" not in selector


# ---------------------------------------------------------------------------
# PVC namespace
# ---------------------------------------------------------------------------


class TestBuildCachePVC:
    def test_pvc_created_in_tenant_namespace(self):
        from kubernetes.client.rest import ApiException

        image = _make_image(org_k8s="myorg", env_k8s="prod")
        mock_core = MagicMock()
        mock_core.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )
        mock_core.create_namespaced_persistent_volume_claim.return_value = MagicMock()

        build_module.fetch_image_build_cache_volume_claim(mock_core, image)

        create_call = mock_core.create_namespaced_persistent_volume_claim.call_args
        assert create_call[0][0] == "cabotage-tenant-builds"

    def test_pvc_read_in_tenant_namespace(self):
        image = _make_image(org_k8s="myorg", env_k8s="prod")
        mock_core = MagicMock()

        build_module.fetch_image_build_cache_volume_claim(mock_core, image)

        read_call = mock_core.read_namespaced_persistent_volume_claim.call_args
        assert read_call[0][1] == "cabotage-tenant-builds"
