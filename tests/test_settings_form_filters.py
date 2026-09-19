"""Tests for whitespace handling on application settings form fields."""

import pytest
from flask import Flask
from werkzeug.datastructures import MultiDict

from cabotage.server.user.forms import (
    EditApplicationEnvironmentSettingsForm,
    EditApplicationSettingsForm,
)


@pytest.fixture
def app():
    """Bare app — these forms only need config for CSRF, no database."""
    _app = Flask(__name__)
    _app.config["TESTING"] = True
    _app.config["SECRET_KEY"] = "test"
    _app.config["WTF_CSRF_ENABLED"] = False
    with _app.app_context():
        yield _app


class TestAutoDeployBranchWhitespace:
    """A pasted branch name with stray whitespace produced unbuildable refs."""

    @pytest.mark.parametrize(
        "submitted,expected",
        [
            ("  main  ", "main"),
            ("main\n", "main"),
            ("\tmain", "main"),
            ("main", "main"),
            ("   ", None),
            ("", None),
        ],
    )
    def test_application_form(self, app, submitted, expected):
        form = EditApplicationSettingsForm(
            formdata=MultiDict({"auto_deploy_branch": submitted})
        )
        assert form.auto_deploy_branch.data == expected

    @pytest.mark.parametrize(
        "submitted,expected",
        [
            ("  main  ", "main"),
            ("main\n", "main"),
            ("\tmain", "main"),
            ("main", "main"),
            ("   ", None),
            ("", None),
        ],
    )
    def test_application_environment_form(self, app, submitted, expected):
        form = EditApplicationEnvironmentSettingsForm(
            formdata=MultiDict({"auto_deploy_branch": submitted})
        )
        assert form.auto_deploy_branch.data == expected

    def test_filters_also_apply_to_existing_records(self, app):
        """Filters run on obj= too, so an already-saved value renders trimmed.

        The stored column keeps its whitespace until the form is submitted.
        """

        class StoredApplication:
            auto_deploy_branch = "  main  "

        form = EditApplicationSettingsForm(obj=StoredApplication())
        assert form.auto_deploy_branch.data == "main"
