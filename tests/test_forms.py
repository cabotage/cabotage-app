import pytest
from flask import Flask
from werkzeug.datastructures import MultiDict

from cabotage.server.user.forms import (
    EditApplicationEnvironmentSettingsForm,
    EditApplicationSettingsForm,
)


@pytest.mark.parametrize(
    "form_class",
    [EditApplicationSettingsForm, EditApplicationEnvironmentSettingsForm],
)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" \tfeature/foo\n", "feature/foo"),
        ("feature/foo", "feature/foo"),
        ("", None),
        (" \t ", None),
        ("feature/foo bar", "feature/foo bar"),
    ],
)
def test_auto_deploy_branch_normalization(form_class, value, expected):
    with Flask(__name__).test_request_context():
        form = form_class(
            MultiDict({"auto_deploy_branch": value}), meta={"csrf": False}
        )
        assert form.auto_deploy_branch.data == expected


@pytest.mark.parametrize(
    "form_class",
    [EditApplicationSettingsForm, EditApplicationEnvironmentSettingsForm],
)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" /health/live", "/health/live"),
        ("\t/_health/ \n", "/_health/"),
        ("/healthz", "/healthz"),
        ("", ""),
    ],
)
def test_health_check_path_strips_whitespace(form_class, value, expected):
    with Flask(__name__).test_request_context():
        form = form_class(MultiDict({"health_check_path": value}), meta={"csrf": False})
        assert form.health_check_path.data == expected
