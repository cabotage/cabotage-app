import pytest
from flask import Flask
from werkzeug.datastructures import MultiDict

from cabotage.server.user.forms import (
    CreateConfigurationForm,
    CreateEnvironmentConfigurationForm,
    EditApplicationEnvironmentSettingsForm,
    EditApplicationSettingsForm,
    EditConfigurationForm,
    EditEnvironmentConfigurationForm,
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
    [
        CreateConfigurationForm,
        EditConfigurationForm,
        CreateEnvironmentConfigurationForm,
        EditEnvironmentConfigurationForm,
    ],
)
@pytest.mark.parametrize(
    ("secure", "value", "valid"),
    [
        (False, "β" * 2048, True),
        (False, "β" * 2049, False),
        (True, "β" * 4096, True),
        (False, "", False),
        (True, "", False),
    ],
)
def test_configuration_value_limit(
    form_class: type[
        CreateConfigurationForm
        | EditConfigurationForm
        | CreateEnvironmentConfigurationForm
        | EditEnvironmentConfigurationForm
    ],
    secure: bool,
    value: str,
    valid: bool,
) -> None:
    with Flask(__name__).test_request_context():
        form = form_class(
            MultiDict({"value": value, "secure": "y" if secure else ""}),
            meta={"csrf": False},
        )
        assert form.value.validate(form) is valid
