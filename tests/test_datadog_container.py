"""Tests for render_datadog_container: image override via datadog_image param."""

from unittest.mock import MagicMock, patch

import pytest

import cabotage.celery.tasks.deploy as deploy_module

DEFAULT_IMAGE = "datadog/agent:7.78.0"


@pytest.fixture
def mock_app():
    app = MagicMock()
    app.config = {"DATADOG_IMAGE": DEFAULT_IMAGE}
    with patch.object(deploy_module, "current_app", app):
        yield app


@pytest.mark.parametrize(
    ("datadog_image", "expected"),
    [
        (None, DEFAULT_IMAGE),
        ("", DEFAULT_IMAGE),
        ("datadog/agent:7.80.0", "datadog/agent:7.80.0"),
    ],
    ids=["none-falls-back", "empty-falls-back", "explicit-overrides"],
)
def test_datadog_image_override(mock_app, datadog_image, expected):
    container = deploy_module.render_datadog_container(
        dd_api_key="secret",
        datadog_tags={"env": "prod"},
        datadog_image=datadog_image,
    )
    assert container.image == expected


def test_positional_call_without_override(mock_app):
    container = deploy_module.render_datadog_container("k", {"env": "p"})
    assert container.image == DEFAULT_IMAGE


def _podspec_release(dd_api_key="secret-key", dd_image=None):
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

    api_obj = MagicMock()
    api_obj.read_value.return_value = dd_api_key
    config = {"DD_API_KEY": api_obj}
    if dd_image is not None:
        img_obj = MagicMock()
        img_obj.read_value.return_value = dd_image
        config["DD_IMAGE"] = img_obj
    release.configuration_objects = config
    return release


@pytest.mark.parametrize(
    ("dd_image", "expected_arg"),
    [(None, None), ("datadog/agent:7.80.0", "datadog/agent:7.80.0")],
    ids=["no-dd-image-config", "dd-image-config-present"],
)
def test_render_podspec_passes_dd_image_to_datadog_container(
    mock_app, dd_image, expected_arg
):
    release = _podspec_release(dd_image=dd_image)
    mock_app.config["SIDECAR_IMAGE"] = "ghcr.io/cabotage/sidecar:1.0"

    with (
        patch.object(deploy_module, "render_datadog_container") as mock_dd,
        patch.object(deploy_module, "render_cabotage_sidecar_container"),
        patch.object(deploy_module, "render_cabotage_sidecar_tls_container"),
        patch.object(deploy_module, "render_process_container"),
        patch.object(deploy_module, "k8s_label_value", return_value="v1"),
    ):
        deploy_module.render_podspec(release, "web", "sa-name")

    assert mock_dd.called
    args, kwargs = mock_dd.call_args
    call_args = list(args) + list(kwargs.values())
    assert "secret-key" in call_args
    assert expected_arg in call_args
