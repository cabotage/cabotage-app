"""Tests for render_cabotage_sidecar_container: startup probe, run_once, TLS."""

from unittest.mock import MagicMock, patch

import cabotage.celery.tasks.deploy as deploy_module


def _make_release():
    release = MagicMock()
    release.application.project.organization.slug = "test-org"
    release.application.project.organization.k8s_identifier = "test-org"
    release.application.project.slug = "test-project"
    release.application.project.k8s_identifier = "test-project"
    release.application.slug = "test-app"
    release.application.k8s_identifier = "test-app"
    release.application.privileged = False
    release.version = 1

    app_env = MagicMock()
    app_env.k8s_identifier = None
    env_obj = MagicMock()
    env_obj.ephemeral = False
    env_obj.slug = "default"
    env_obj.k8s_identifier = "default"
    app_env.environment = env_obj
    release.application_environment = app_env
    release.configuration_objects = {}

    return release


def _render(release=None, process_name="web", with_tls=True, run_once=False):
    release = release or _make_release()
    mock_app = MagicMock()
    mock_app.config = {"SIDECAR_IMAGE": "ghcr.io/cabotage/containers/sidecar-rs:1.0"}
    with patch.object(deploy_module, "current_app", mock_app):
        return deploy_module.render_cabotage_sidecar_container(
            release, process_name, with_tls=with_tls, run_once=run_once
        )


class TestStartupProbe:
    def test_has_startup_probe(self):
        container = _render()
        assert container.startup_probe is not None

    def test_startup_probe_checks_vault_and_consul_tokens(self):
        container = _render()
        cmd = container.startup_probe._exec.command
        assert cmd[0] == "sh"
        assert "vault-token" in cmd[2]
        assert "consul-token" in cmd[2]

    def test_startup_probe_present_for_run_once(self):
        container = _render(run_once=True)
        assert container.startup_probe is not None

    def test_startup_probe_period_and_threshold(self):
        container = _render()
        assert container.startup_probe.period_seconds == 1
        assert container.startup_probe.failure_threshold == 30


class TestRunOnce:
    def test_run_once_uses_kube_login(self):
        container = _render(run_once=True)
        assert container.args[0] == "kube-login"

    def test_run_once_still_has_restart_always(self):
        container = _render(run_once=True)
        assert container.restart_policy == "Always"

    def test_long_lived_uses_kube_login_and_maintain(self):
        container = _render(run_once=False)
        assert container.args[0] == "kube-login-and-maintain"

    def test_long_lived_restart_policy_always(self):
        container = _render(run_once=False)
        assert container.restart_policy == "Always"


class TestTLS:
    def test_with_tls_includes_cert_args(self):
        container = _render(with_tls=True, process_name="web")
        assert "--fetch-cert" in container.args
        assert any(a.startswith("--vault-pki-role=") for a in container.args)
        assert any(a.endswith("-web") for a in container.args)

    def test_without_tls_no_cert_args(self):
        container = _render(with_tls=False)
        assert "--fetch-cert" not in container.args


class TestPodspecIntegration:
    """Verify render_podspec wires run_once correctly per process type."""

    _DEPLOY_MODULE = "cabotage.celery.tasks.deploy"

    def _render_podspec(self, process_name):
        release = _make_release()
        release.application.privileged = False
        release.configuration_objects = {}
        mock_app = MagicMock()
        mock_app.config = {
            "SIDECAR_IMAGE": "ghcr.io/cabotage/containers/sidecar-rs:1.0",
        }
        with (
            patch.object(deploy_module, "current_app", mock_app),
            patch(
                f"{self._DEPLOY_MODULE}.render_cabotage_sidecar_tls_container",
                return_value=MagicMock(),
            ),
            patch(
                f"{self._DEPLOY_MODULE}.render_process_container",
                return_value=MagicMock(),
            ),
            patch(f"{self._DEPLOY_MODULE}.k8s_label_value", return_value="v1"),
        ):
            return deploy_module.render_podspec(release, process_name, "sa-name")

    def _sidecar_init(self, podspec):
        for c in podspec.init_containers:
            if hasattr(c, "name") and c.name == "cabotage-sidecar":
                return c
        return None

    def test_web_uses_kube_login_and_maintain(self):
        podspec = self._render_podspec("web")
        sidecar = self._sidecar_init(podspec)
        assert sidecar is not None
        assert sidecar.args[0] == "kube-login-and-maintain"
        assert sidecar.restart_policy == "Always"

    def test_worker_uses_kube_login_and_maintain(self):
        podspec = self._render_podspec("worker")
        sidecar = self._sidecar_init(podspec)
        assert sidecar is not None
        assert sidecar.args[0] == "kube-login-and-maintain"
        assert sidecar.restart_policy == "Always"

    def test_release_uses_kube_login(self):
        podspec = self._render_podspec("release")
        sidecar = self._sidecar_init(podspec)
        assert sidecar is not None
        assert sidecar.args[0] == "kube-login"
        assert sidecar.restart_policy == "Always"

    def test_postdeploy_uses_kube_login(self):
        podspec = self._render_podspec("postdeploy")
        sidecar = self._sidecar_init(podspec)
        assert sidecar is not None
        assert sidecar.args[0] == "kube-login"
        assert sidecar.restart_policy == "Always"

    def test_job_uses_kube_login(self):
        podspec = self._render_podspec("job-cleanup")
        sidecar = self._sidecar_init(podspec)
        assert sidecar is not None
        assert sidecar.args[0] == "kube-login"
        assert sidecar.restart_policy == "Always"
