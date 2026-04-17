"""Tests for render_datadog_container: image override via datadog_image param."""

from unittest.mock import MagicMock, patch

import cabotage.celery.tasks.deploy as deploy_module


def _render(datadog_image=None):
    mock_app = MagicMock()
    mock_app.config = {"DATADOG_IMAGE": "gcr.io/datadoghq/dogstatsd:default"}
    with patch.object(deploy_module, "current_app", mock_app):
        return deploy_module.render_datadog_container(
            dd_api_key="secret",
            datadog_tags={"env": "prod", "service": "api"},
            datadog_image=datadog_image,
        )


class TestDatadogImageOverride:
    def test_default_uses_app_config(self):
        container = _render()
        assert container.image == "gcr.io/datadoghq/dogstatsd:default"

    def test_none_falls_back_to_app_config(self):
        container = _render(datadog_image=None)
        assert container.image == "gcr.io/datadoghq/dogstatsd:default"

    def test_explicit_image_overrides_app_config(self):
        container = _render(datadog_image="custom.registry/dogstatsd:7.50.0")
        assert container.image == "custom.registry/dogstatsd:7.50.0"

    def test_empty_string_falls_back_to_app_config(self):
        container = _render(datadog_image="")
        assert container.image == "gcr.io/datadoghq/dogstatsd:default"

    def test_positional_call_without_override(self):
        mock_app = MagicMock()
        mock_app.config = {"DATADOG_IMAGE": "gcr.io/datadoghq/dogstatsd:default"}
        with patch.object(deploy_module, "current_app", mock_app):
            container = deploy_module.render_datadog_container("k", {"env": "p"})
        assert container.image == "gcr.io/datadoghq/dogstatsd:default"
