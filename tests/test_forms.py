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
