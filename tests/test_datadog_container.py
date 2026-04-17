"""Tests for render_datadog_container: image override via datadog_image param."""

from unittest.mock import MagicMock, patch

import pytest

import cabotage.celery.tasks.deploy as deploy_module

DEFAULT_IMAGE = "gcr.io/datadoghq/dogstatsd:default"


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
        ("custom.registry/dogstatsd:7.50.0", "custom.registry/dogstatsd:7.50.0"),
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
