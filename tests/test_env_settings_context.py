"""Environment context on application-wide settings (issue #376).

Reported: from a non-default environment, Settings -> Application Settings
dropped you into the default environment, where delete removes the application
from every environment.
"""

import datetime
from unittest.mock import patch

import pytest

from cabotage.server.models.projects import ApplicationEnvironment
from cabotage.server.user import github_installations
from cabotage.server.user.views import (
    _app_env_for_env_slug,
    _render_application_settings,
    _settings_env_context,
)

ALL_OVERRIDES = {
    "auto_deploy_branch": "feature-x",
    "github_environment_name": "test-env",
    "deployment_timeout": 600,
    "health_check_path": "/healthz",
    "health_check_host": "example.com",
}


@pytest.fixture
def env_project(project):
    """#376 only arises in environment-enabled projects."""
    project.environments_enabled = True
    return project


@pytest.fixture
def envs(make_environment):
    return {
        "production": make_environment("production", is_default=True),
        "staging": make_environment("staging"),
    }


@pytest.fixture
def enrolled(application, envs, make_app_env):
    return {slug: make_app_env(application, env) for slug, env in envs.items()}


@pytest.fixture
def render_context(app, org, application):
    """Context handed to the settings template for a given env context."""

    def _render(environment, env_context):
        with (
            app.test_request_context("/"),
            patch("cabotage.server.user.views.render_template") as render_template,
        ):
            _render_application_settings(
                application, org, None, environment, env_context
            )
        return render_template.call_args.kwargs

    return _render


class TestSettingsEnvContext:
    def test_keeps_the_environment_you_came_from(
        self, application, env_project, enrolled
    ):
        environment, env_context = _settings_env_context(
            application, env_project, "staging"
        )

        assert environment.slug == "staging"
        assert env_context is enrolled["staging"]

    def test_arriving_without_an_environment_uses_the_default(
        self, application, env_project, enrolled
    ):
        environment, env_context = _settings_env_context(application, env_project, None)

        assert environment.slug == "production"
        assert env_context is None

    def test_unknown_environment_falls_back_instead_of_aborting(
        self, application, env_project, enrolled
    ):
        environment, env_context = _settings_env_context(
            application, env_project, "deleted-env"
        )

        assert environment.slug == "production"
        assert env_context is None


class TestDeleteIsOnlyOfferedAppWide:
    def test_withheld_when_viewing_from_an_environment(
        self, application, env_project, enrolled, render_context
    ):
        environment, env_context = _settings_env_context(
            application, env_project, "staging"
        )

        ctx = render_context(environment, env_context)

        assert ctx["delete_form"] is None
        assert ctx["delete_impact"] is None
        assert ctx["env_context"] is env_context

    def test_offered_on_the_app_wide_page(
        self, application, env_project, enrolled, render_context
    ):
        environment, env_context = _settings_env_context(application, env_project, None)

        ctx = render_context(environment, env_context)

        assert ctx["delete_form"] is not None
        assert ctx["env_context"] is None


class TestGitHubInstallKeepsTheEnvironment:
    """The install round trip must not drop you back into the default env."""

    def test_install_link_carries_the_environment(
        self, application, env_project, enrolled, render_context
    ):
        environment, env_context = _settings_env_context(
            application, env_project, "staging"
        )

        ctx = render_context(environment, env_context)

        assert "env_slug=staging" in ctx["app_url"]

    def test_install_link_has_no_environment_when_app_wide(
        self, application, env_project, enrolled, render_context
    ):
        environment, env_context = _settings_env_context(application, env_project, None)

        ctx = render_context(environment, env_context)

        assert "env_slug" not in ctx["app_url"]

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [({"env_slug": "staging"}, "staging"), ({}, None)],
        ids=["from an environment", "app-wide"],
    )
    def test_signed_state_round_trips_the_environment(self, app, org, kwargs, expected):
        with app.test_request_context("/"):
            state = github_installations.install_state(org, "a-user-id", **kwargs)
            payload = github_installations.install_state_serializer().loads(state)

        assert payload.get("env_slug") == expected


class TestAppEnvForEnvSlug:
    """Display-only hint: resolution degrades, never aborts."""

    def test_resolves_a_known_slug(self, application, environment, make_app_env):
        app_env = make_app_env(application, environment)

        assert _app_env_for_env_slug(application, "default") is app_env

    def test_unknown_slug_returns_none(self, application, environment, make_app_env):
        make_app_env(application, environment)

        assert _app_env_for_env_slug(application, "no-such-env") is None

    @pytest.mark.parametrize("slug", [None, ""])
    def test_missing_slug_returns_none(
        self, application, environment, make_app_env, slug
    ):
        make_app_env(application, environment)

        assert _app_env_for_env_slug(application, slug) is None

    def test_ignores_soft_deleted_enrolment(
        self, application, environment, make_app_env
    ):
        make_app_env(
            application,
            environment,
            deleted_at=datetime.datetime.now(datetime.timezone.utc),
        )

        assert _app_env_for_env_slug(application, "default") is None


class TestOverriddenSettings:
    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({}, []),
            (
                {"auto_deploy_branch": "feature-x", "deployment_timeout": 600},
                ["Branch", "Deploy Timeout"],
            ),
            (
                ALL_OVERRIDES,
                [
                    "Branch",
                    "GitHub Environment",
                    "Deploy Timeout",
                    "Health Path",
                    "Health Host",
                ],
            ),
        ],
        ids=["none", "some", "all"],
    )
    def test_reports_overridden_labels(
        self, application, environment, make_app_env, overrides, expected
    ):
        app_env = make_app_env(application, environment, **overrides)

        assert app_env.overridden_settings == expected

    def test_labels_stay_in_step_with_the_effective_properties(self):
        """An override nothing reads would be a lie on the page."""
        for attr, _ in ApplicationEnvironment.OVERRIDABLE_SETTINGS:
            assert hasattr(ApplicationEnvironment, f"effective_{attr}")

    def test_branch_override_means_the_app_value_is_not_used(
        self, db_session, application, environment, make_app_env
    ):
        application.auto_deploy_branch = "main"
        db_session.flush()
        app_env = make_app_env(application, environment, auto_deploy_branch="feature-x")

        assert "Branch" in app_env.overridden_settings
        assert app_env.effective_auto_deploy_branch == "feature-x"
        assert application.auto_deploy_branch == "main"
