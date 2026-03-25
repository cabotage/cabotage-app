"""Tests for Fargate preview namespace support."""

import pytest

from cabotage.celery.tasks.deploy import (
    k8s_namespace,
    k8s_resource_prefix,
    render_namespace,
)
from cabotage.server.wsgi import app as _app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    with _app.app_context():
        yield _app


# ---------------------------------------------------------------------------
# Fake model objects
# ---------------------------------------------------------------------------


def _make_release(ephemeral=False):
    """Build a fake release with configurable ephemeral flag on environment."""

    class Org:
        slug = "myorg"
        k8s_identifier = "myorg-abc123"

    class Project:
        organization = Org()
        slug = "myproject"
        k8s_identifier = "myproject-def456"

    class App:
        project = Project()
        slug = "myapp"
        k8s_identifier = "myapp-ghi789"

    class Env:
        slug = "pr-42"
        k8s_identifier = "pr-42-jkl012"
        ephemeral = False  # placeholder, overridden below

    Env.ephemeral = ephemeral

    class AppEnv:
        k8s_identifier = "appenv-123"
        environment = Env()
        process_counts = {"web": 1}
        process_pod_classes = {}

    class Release:
        application = App()
        application_environment = AppEnv()
        version = 1

    return Release()


# ---------------------------------------------------------------------------
# k8s_namespace
# ---------------------------------------------------------------------------


class TestK8sNamespaceFargate:
    def test_non_ephemeral_ignores_fargate_config(self, app):
        app.config["FARGATE_PREVIEW_NAMESPACE"] = "cabotage-previews"
        release = _make_release(ephemeral=False)
        ns = k8s_namespace(release)
        # Should use the normal org+env namespace, not the fargate one
        assert ns != "cabotage-previews"
        assert "myorg" in ns

    def test_ephemeral_without_config_uses_normal_namespace(self, app):
        app.config["FARGATE_PREVIEW_NAMESPACE"] = None
        release = _make_release(ephemeral=True)
        ns = k8s_namespace(release)
        assert ns != "cabotage-previews"
        assert "myorg" in ns

    def test_ephemeral_with_config_uses_fargate_namespace(self, app):
        app.config["FARGATE_PREVIEW_NAMESPACE"] = "cabotage-previews"
        release = _make_release(ephemeral=True)
        ns = k8s_namespace(release)
        assert ns == "cabotage-previews"


# ---------------------------------------------------------------------------
# k8s_resource_prefix
# ---------------------------------------------------------------------------


class TestK8sResourcePrefixFargate:
    def test_non_ephemeral_prefix_unchanged(self, app):
        app.config["FARGATE_PREVIEW_NAMESPACE"] = "cabotage-previews"
        release = _make_release(ephemeral=False)
        prefix = k8s_resource_prefix(release)
        # Should be project-app, no env identifier
        assert "pr-42" not in prefix
        assert "myproject" in prefix
        assert "myapp" in prefix

    def test_ephemeral_without_config_prefix_unchanged(self, app):
        app.config["FARGATE_PREVIEW_NAMESPACE"] = None
        release = _make_release(ephemeral=True)
        prefix = k8s_resource_prefix(release)
        assert "pr-42" not in prefix

    def test_ephemeral_with_config_includes_env_in_prefix(self, app):
        app.config["FARGATE_PREVIEW_NAMESPACE"] = "cabotage-previews"
        release = _make_release(ephemeral=True)
        prefix = k8s_resource_prefix(release)
        # Should include env k8s_identifier to disambiguate PRs
        assert "pr-42" in prefix
        assert "myproject" in prefix
        assert "myapp" in prefix

    def test_different_prs_get_different_prefixes(self, app):
        app.config["FARGATE_PREVIEW_NAMESPACE"] = "cabotage-previews"
        r1 = _make_release(ephemeral=True)
        r2 = _make_release(ephemeral=True)
        r2.application_environment.environment.k8s_identifier = "pr-99-mno345"
        r2.application_environment.environment.slug = "pr-99"
        assert k8s_resource_prefix(r1) != k8s_resource_prefix(r2)


# ---------------------------------------------------------------------------
# render_namespace uses fargate namespace
# ---------------------------------------------------------------------------


class TestRenderNamespaceFargate:
    def test_render_namespace_uses_fargate_ns(self, app):
        app.config["FARGATE_PREVIEW_NAMESPACE"] = "cabotage-previews"
        release = _make_release(ephemeral=True)
        ns = render_namespace(release)
        assert ns.metadata.name == "cabotage-previews"

    def test_render_namespace_normal_when_not_configured(self, app):
        app.config["FARGATE_PREVIEW_NAMESPACE"] = None
        release = _make_release(ephemeral=True)
        ns = render_namespace(release)
        assert ns.metadata.name != "cabotage-previews"


# ---------------------------------------------------------------------------
# Deployment naming: resource prefix becomes the deployment name stem
# ---------------------------------------------------------------------------


class TestDeploymentNamingFargate:
    def test_deployment_name_includes_env_in_shared_ns(self, app):
        """Deployment name = {resource_prefix}-{process}.

        When using the shared Fargate namespace, the resource prefix includes
        the env identifier so two PRs don't collide.
        """
        app.config["FARGATE_PREVIEW_NAMESPACE"] = "cabotage-previews"
        release = _make_release(ephemeral=True)
        prefix = k8s_resource_prefix(release)
        deployment_name = f"{prefix}-web"
        assert "pr-42" in deployment_name

    def test_deployment_name_no_env_without_fargate(self, app):
        app.config["FARGATE_PREVIEW_NAMESPACE"] = None
        release = _make_release(ephemeral=True)
        prefix = k8s_resource_prefix(release)
        deployment_name = f"{prefix}-web"
        assert "pr-42" not in deployment_name
