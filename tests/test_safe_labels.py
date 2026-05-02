"""Tests for cabotage.io/ prefixed safe labels on k8s resources.

Verifies that all render_* functions include cabotage.io/{organization,project,
application,environment} labels derived from k8s_identifiers.
"""

from unittest.mock import MagicMock, patch

import kubernetes.client
import pytest

import cabotage.celery.tasks.deploy as deploy_module
from cabotage.celery.tasks.deploy import (
    _safe_labels_from_application,
    _safe_labels_from_release,
    k8s_label_value,
    render_ingress_object,
    render_service,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEPLOY_MODULE = "cabotage.celery.tasks.deploy"
_PATCHES = [
    f"{_DEPLOY_MODULE}.render_cabotage_sidecar_container",
    f"{_DEPLOY_MODULE}.render_cabotage_sidecar_tls_container",
    f"{_DEPLOY_MODULE}.render_process_container",
    f"{_DEPLOY_MODULE}.render_datadog_container",
    f"{_DEPLOY_MODULE}.k8s_label_value",
]

SAFE_LABEL_KEYS = {
    "cabotage.io/organization",
    "cabotage.io/project",
    "cabotage.io/application",
}


def _make_release(env_enabled=True):
    release = MagicMock()
    release.application.project.organization.slug = "test-org"
    release.application.project.organization.k8s_identifier = "test-org-a1b2c3"
    release.application.project.slug = "test-project"
    release.application.project.k8s_identifier = "test-project-d4e5f6"
    release.application.slug = "test-app"
    release.application.k8s_identifier = "test-app-g7h8i9"
    release.application.privileged = False
    release.version = 1

    app_env = MagicMock()
    if env_enabled:
        app_env.k8s_identifier = "appenv-j0k1l2"
        env_obj = MagicMock()
        env_obj.slug = "production"
        env_obj.k8s_identifier = "production-m3n4o5"
        env_obj.uses_environment_namespace = True
        env_obj.ephemeral = False
        app_env.environment = env_obj
    else:
        app_env.k8s_identifier = None
        env_obj = MagicMock()
        env_obj.slug = "default"
        env_obj.k8s_identifier = None
        env_obj.uses_environment_namespace = False
        env_obj.ephemeral = False
        app_env.environment = env_obj

    app_env.process_counts = {"web": 2}
    app_env.process_pod_classes = {}
    app_env.application = release.application
    release.application_environment = app_env
    release.configuration_objects = {}
    release.job_processes = {
        "job-cleanup": {
            "cmd": "python cleanup.py",
            "env": [("SCHEDULE", "0 * * * *")],
        }
    }

    return release


def _make_image():
    image = MagicMock()
    image.application.project.organization.slug = "test-org"
    image.application.project.organization.k8s_identifier = "test-org-a1b2c3"
    image.application.project.slug = "test-project"
    image.application.project.k8s_identifier = "test-project-d4e5f6"
    image.application.slug = "test-app"
    image.application.k8s_identifier = "test-app-g7h8i9"
    return image


@pytest.fixture(autouse=True)
def _mock_sub_renderers():
    patchers = []
    for target in _PATCHES:
        p = patch(target, return_value=MagicMock(spec=kubernetes.client.V1Container))
        p.start()
        patchers.append(p)
    yield
    for p in patchers:
        p.stop()


@pytest.fixture()
def mock_app():
    mock = MagicMock()
    mock.config = {}
    with patch.object(deploy_module, "current_app", mock):
        yield mock


def _assert_safe_labels(labels, env_expected=True):
    """Assert that the cabotage.io/ safe labels are present with correct values."""
    assert labels["cabotage.io/organization"] == "test-org-a1b2c3"
    assert labels["cabotage.io/project"] == "test-project-d4e5f6"
    assert labels["cabotage.io/application"] == "test-app-g7h8i9"
    if env_expected:
        assert labels["cabotage.io/environment"] == "production-m3n4o5"
    else:
        assert "cabotage.io/environment" not in labels


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestSafeLabelsFromRelease:
    def test_includes_all_labels_with_environment(self):
        release = _make_release(env_enabled=True)
        labels = _safe_labels_from_release(release)
        _assert_safe_labels(labels, env_expected=True)

    def test_excludes_environment_when_legacy(self):
        release = _make_release(env_enabled=False)
        labels = _safe_labels_from_release(release)
        _assert_safe_labels(labels, env_expected=False)

    def test_includes_environment_when_env_mode_enabled_even_if_app_env_is_legacy(self):
        release = _make_release(env_enabled=False)
        release.application_environment.environment.slug = "production"
        release.application_environment.environment.k8s_identifier = "production-m3n4o5"
        release.application_environment.environment.uses_environment_namespace = True

        labels = _safe_labels_from_release(release)

        _assert_safe_labels(labels, env_expected=True)


class TestK8sLabelValue:
    def test_includes_environment_when_env_mode_enabled_even_if_app_env_is_legacy(self):
        release = _make_release(env_enabled=False)
        release.application_environment.environment.slug = "production"
        release.application_environment.environment.k8s_identifier = "production-m3n4o5"
        release.application_environment.environment.uses_environment_namespace = True

        label_value = k8s_label_value(release)

        assert "production" in label_value

    def test_excludes_environment_when_env_mode_disabled_even_if_app_env_is_set(self):
        release = _make_release(env_enabled=True)
        release.application_environment.environment.uses_environment_namespace = False

        label_value = k8s_label_value(release)

        assert "production" not in label_value


class TestSafeLabelsFromApplication:
    def test_includes_org_project_application(self):
        image = _make_image()
        labels = _safe_labels_from_application(image.application)
        assert labels["cabotage.io/organization"] == "test-org-a1b2c3"
        assert labels["cabotage.io/project"] == "test-project-d4e5f6"
        assert labels["cabotage.io/application"] == "test-app-g7h8i9"
        assert "cabotage.io/environment" not in labels


# ---------------------------------------------------------------------------
# render_deployment
# ---------------------------------------------------------------------------


class TestRenderDeploymentSafeLabels:
    def test_pod_labels_have_safe_labels(self, mock_app):
        release = _make_release()
        with patch(f"{_DEPLOY_MODULE}.k8s_resource_prefix", return_value="proj-app"):
            dep = deploy_module.render_deployment(
                "test-ns", release, "sa-name", "web", "deploy-id"
            )
        pod_labels = dep.spec.template.metadata.labels
        _assert_safe_labels(pod_labels)

    def test_deployment_metadata_has_safe_labels(self, mock_app):
        release = _make_release()
        with patch(f"{_DEPLOY_MODULE}.k8s_resource_prefix", return_value="proj-app"):
            dep = deploy_module.render_deployment(
                "test-ns", release, "sa-name", "web", "deploy-id"
            )
        _assert_safe_labels(dep.metadata.labels)

    def test_no_environment_label_for_legacy(self, mock_app):
        release = _make_release(env_enabled=False)
        with patch(f"{_DEPLOY_MODULE}.k8s_resource_prefix", return_value="proj-app"):
            dep = deploy_module.render_deployment(
                "test-ns", release, "sa-name", "web", "deploy-id"
            )
        assert "cabotage.io/environment" not in dep.spec.template.metadata.labels
        assert "cabotage.io/environment" not in dep.metadata.labels


# ---------------------------------------------------------------------------
# render_service
# ---------------------------------------------------------------------------


class TestRenderServiceSafeLabels:
    def test_service_metadata_has_safe_labels(self):
        release = _make_release()
        svc = render_service(release, "web")
        _assert_safe_labels(svc.metadata.labels)


# ---------------------------------------------------------------------------
# render_ingress
# ---------------------------------------------------------------------------


class FakeHost:
    def __init__(self, hostname, tls_enabled=True, is_auto_generated=False):
        self.hostname = hostname
        self.tls_enabled = tls_enabled
        self.is_auto_generated = is_auto_generated


class FakePath:
    def __init__(self, path="/", path_type="Prefix", target_process_name="web"):
        self.path = path
        self.path_type = path_type
        self.target_process_name = target_process_name


class FakeIngress:
    def __init__(self, **kwargs):
        self.name = kwargs.get("name", "web")
        self.enabled = kwargs.get("enabled", True)
        self.ingress_class_name = kwargs.get("ingress_class_name", "nginx")
        self.backend_protocol = kwargs.get("backend_protocol", "HTTPS")
        self.proxy_connect_timeout = kwargs.get("proxy_connect_timeout", "10s")
        self.proxy_read_timeout = kwargs.get("proxy_read_timeout", "10s")
        self.proxy_send_timeout = kwargs.get("proxy_send_timeout", "10s")
        self.proxy_body_size = kwargs.get("proxy_body_size", "10M")
        self.client_body_buffer_size = kwargs.get("client_body_buffer_size", "1M")
        self.proxy_request_buffering = kwargs.get("proxy_request_buffering", "on")
        self.session_affinity = kwargs.get("session_affinity", False)
        self.use_regex = kwargs.get("use_regex", False)
        self.allow_annotations = kwargs.get("allow_annotations", False)
        self.extra_annotations = kwargs.get("extra_annotations", {})
        self.cluster_issuer = kwargs.get("cluster_issuer", "letsencrypt")
        self.force_ssl_redirect = kwargs.get("force_ssl_redirect", True)
        self.service_upstream = kwargs.get("service_upstream", True)
        self.tailscale_hostname = kwargs.get("tailscale_hostname", None)
        self.tailscale_funnel = kwargs.get("tailscale_funnel", False)
        self.tailscale_tags = kwargs.get("tailscale_tags", None)
        self.hosts = kwargs.get("hosts", [])
        self.paths = kwargs.get("paths", [])


class TestRenderIngressSafeLabels:
    def test_ingress_labels_include_safe_labels(self):
        ing = FakeIngress(
            hosts=[FakeHost("example.com")],
            paths=[FakePath()],
        )
        labels = {
            "organization": "test-org",
            "project": "test-project",
            "application": "test-app",
            "app": "test-label",
            "cabotage.io/organization": "test-org-a1b2c3",
            "cabotage.io/project": "test-project-d4e5f6",
            "cabotage.io/application": "test-app-g7h8i9",
        }
        obj = render_ingress_object(ing, "proj-app", labels)
        assert obj.metadata.labels["cabotage.io/organization"] == "test-org-a1b2c3"
        assert obj.metadata.labels["cabotage.io/project"] == "test-project-d4e5f6"
        assert obj.metadata.labels["cabotage.io/application"] == "test-app-g7h8i9"


# ---------------------------------------------------------------------------
# render_job
# ---------------------------------------------------------------------------


class TestRenderJobSafeLabels:
    def test_job_metadata_has_safe_labels(self, mock_app):
        release = _make_release()
        dep = deploy_module.render_job(
            "test-ns", release, "sa-name", "web", "job-id-123"
        )
        _assert_safe_labels(dep.metadata.labels)

    def test_job_pod_template_has_safe_labels(self, mock_app):
        release = _make_release()
        dep = deploy_module.render_job(
            "test-ns", release, "sa-name", "web", "job-id-123"
        )
        pod_labels = dep.spec.template.metadata.labels
        _assert_safe_labels(pod_labels)


# ---------------------------------------------------------------------------
# render_cronjob
# ---------------------------------------------------------------------------


class TestRenderCronjobSafeLabels:
    def test_cronjob_metadata_has_safe_labels(self, mock_app):
        release = _make_release()
        with patch(f"{_DEPLOY_MODULE}.k8s_resource_prefix", return_value="proj-app"):
            cj = deploy_module.render_cronjob(
                "test-ns", release, "sa-name", "job-cleanup", "deploy-123"
            )
        _assert_safe_labels(cj.metadata.labels)

    def test_cronjob_job_template_has_safe_labels(self, mock_app):
        release = _make_release()
        with patch(f"{_DEPLOY_MODULE}.k8s_resource_prefix", return_value="proj-app"):
            cj = deploy_module.render_cronjob(
                "test-ns", release, "sa-name", "job-cleanup", "deploy-123"
            )
        job_labels = cj.spec.job_template.metadata.labels
        _assert_safe_labels(job_labels)

    def test_cronjob_pod_template_has_safe_labels(self, mock_app):
        release = _make_release()
        with patch(f"{_DEPLOY_MODULE}.k8s_resource_prefix", return_value="proj-app"):
            cj = deploy_module.render_cronjob(
                "test-ns", release, "sa-name", "job-cleanup", "deploy-123"
            )
        pod_labels = cj.spec.job_template.spec.template.metadata.labels
        _assert_safe_labels(pod_labels)
