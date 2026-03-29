"""Tests for render_podspec node pool selector/toleration behavior.

Verifies that PREVIEW_POOL and STANDARD_POOL config vars control
node selectors and tolerations on pod specs.
"""

from unittest.mock import MagicMock, patch

import kubernetes.client
import pytest

import cabotage.celery.tasks.deploy as deploy_module


def _make_release(ephemeral=False, has_dd_key=False):
    """Build a mock release object with enough structure for render_podspec."""
    release = MagicMock()
    release.application.project.organization.slug = "test-org"
    release.application.project.slug = "test-project"
    release.application.slug = "test-app"
    release.version = 1

    app_env = MagicMock()
    env_obj = MagicMock()
    env_obj.ephemeral = ephemeral

    if ephemeral:
        app_env.k8s_identifier = "some-id"
        app_env.environment = env_obj
    else:
        app_env.k8s_identifier = None
        app_env.environment = env_obj

    release.application_environment = app_env

    if has_dd_key:
        dd_mock = MagicMock()
        release.configuration_objects = {"DD_API_KEY": dd_mock}
    else:
        release.configuration_objects = {}

    return release


_DEPLOY_MODULE = "cabotage.celery.tasks.deploy"
_PATCHES = [
    f"{_DEPLOY_MODULE}.render_cabotage_enroller_container",
    f"{_DEPLOY_MODULE}.render_cabotage_sidecar_container",
    f"{_DEPLOY_MODULE}.render_cabotage_sidecar_tls_container",
    f"{_DEPLOY_MODULE}.render_process_container",
    f"{_DEPLOY_MODULE}.render_datadog_container",
    f"{_DEPLOY_MODULE}.k8s_label_value",
]


@pytest.fixture(autouse=True)
def _mock_sub_renderers():
    """Mock all sub-render functions to return simple placeholders."""
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


class TestRenderPodspecNodePoolConfigured:
    """When STANDARD_POOL / PREVIEW_POOL are set, selectors and tolerations are applied."""

    def test_standard_pool_for_non_ephemeral(self, mock_app):
        mock_app.config["STANDARD_POOL"] = "standard"

        release = _make_release(ephemeral=False)
        pod_spec = deploy_module.render_podspec(release, "worker", "sa-name")

        assert pod_spec.node_selector == {"cabotage.dev/node-pool": "standard"}
        assert len(pod_spec.tolerations) == 1
        tol = pod_spec.tolerations[0]
        assert tol.key == "cabotage.dev/node-pool"
        assert tol.value == "standard"
        assert tol.effect == "NoSchedule"

    def test_preview_pool_for_ephemeral(self, mock_app):
        mock_app.config["PREVIEW_POOL"] = "preview"

        release = _make_release(ephemeral=True)
        pod_spec = deploy_module.render_podspec(release, "worker", "sa-name")

        assert pod_spec.node_selector == {"cabotage.dev/node-pool": "preview"}
        assert len(pod_spec.tolerations) == 1
        tol = pod_spec.tolerations[0]
        assert tol.key == "cabotage.dev/node-pool"
        assert tol.value == "preview"
        assert tol.effect == "NoSchedule"

    def test_standard_pool_with_k8s_identifier_set(self, mock_app):
        """Non-ephemeral env with k8s_identifier set should still get standard."""
        mock_app.config["STANDARD_POOL"] = "standard"

        release = _make_release(ephemeral=False)
        release.application_environment.k8s_identifier = "some-id"
        release.application_environment.environment.ephemeral = False
        pod_spec = deploy_module.render_podspec(release, "web", "sa-name")

        assert pod_spec.node_selector == {"cabotage.dev/node-pool": "standard"}
        assert pod_spec.tolerations[0].value == "standard"

    def test_node_selector_present_for_all_process_types(self, mock_app):
        """Verify node_selector is set regardless of process type."""
        mock_app.config["STANDARD_POOL"] = "standard"

        for proc in ["web", "worker", "release", "postdeploy", "tcp", "job", "other"]:
            release = _make_release(ephemeral=False)
            pod_spec = deploy_module.render_podspec(release, proc, "sa-name")
            assert pod_spec.node_selector == {
                "cabotage.dev/node-pool": "standard"
            }, f"node_selector wrong for process type {proc}"

    def test_custom_pool_names(self, mock_app):
        """Pool names are not hardcoded — config values are used directly."""
        mock_app.config["STANDARD_POOL"] = "my-custom-pool"

        release = _make_release(ephemeral=False)
        pod_spec = deploy_module.render_podspec(release, "worker", "sa-name")

        assert pod_spec.node_selector == {"cabotage.dev/node-pool": "my-custom-pool"}
        assert pod_spec.tolerations[0].value == "my-custom-pool"


class TestRenderPodspecNodePoolUnset:
    """When STANDARD_POOL / PREVIEW_POOL are not set, no selector or toleration is applied."""

    def test_no_selector_when_standard_pool_unset(self, mock_app):
        release = _make_release(ephemeral=False)
        pod_spec = deploy_module.render_podspec(release, "worker", "sa-name")

        assert pod_spec.node_selector is None
        assert pod_spec.tolerations is None

    def test_no_selector_when_preview_pool_unset(self, mock_app):
        release = _make_release(ephemeral=True)
        pod_spec = deploy_module.render_podspec(release, "worker", "sa-name")

        assert pod_spec.node_selector is None
        assert pod_spec.tolerations is None

    def test_no_selector_when_pool_is_empty_string(self, mock_app):
        """Empty string should be treated same as unset."""
        mock_app.config["STANDARD_POOL"] = ""

        release = _make_release(ephemeral=False)
        pod_spec = deploy_module.render_podspec(release, "worker", "sa-name")

        assert pod_spec.node_selector is None
        assert pod_spec.tolerations is None

    def test_independent_config_keys(self, mock_app):
        """Setting STANDARD_POOL doesn't affect ephemeral, and vice versa."""
        mock_app.config["STANDARD_POOL"] = "standard"
        # PREVIEW_POOL intentionally not set

        # Non-ephemeral gets the selector
        release = _make_release(ephemeral=False)
        pod_spec = deploy_module.render_podspec(release, "worker", "sa-name")
        assert pod_spec.node_selector == {"cabotage.dev/node-pool": "standard"}

        # Ephemeral does not
        release = _make_release(ephemeral=True)
        pod_spec = deploy_module.render_podspec(release, "worker", "sa-name")
        assert pod_spec.node_selector is None
