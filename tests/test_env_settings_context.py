"""Environment context on application-wide settings (issue #376).

Reported: from a non-default environment, Settings -> Application Settings
dropped you into the default environment, where delete removes the application
from every environment.
"""

from unittest.mock import patch

import pytest

from cabotage.server.models.projects import ApplicationEnvironment
from cabotage.server.user.views import (
    _app_env_for_env_slug,
    _render_application_settings,
    _settings_env_context,
)


@pytest.fixture
def env_project(db_session, project):
    """#376 only arises in environment-enabled projects."""
    project.environments_enabled = True
    db_session.flush()
    return project


@pytest.fixture
def prod_and_staging(make_environment):
    return (
        make_environment("production", is_default=True),
        make_environment("staging"),
    )


@pytest.fixture
def enrolled_everywhere(application, prod_and_staging, make_app_env):
    prod, staging = prod_and_staging
    return {
        "production": make_app_env(application, prod),
        "staging": make_app_env(application, staging),
    }


class TestSettingsEnvContext:
    def test_keeps_the_environment_you_came_from(
        self, db_session, application, env_project, enrolled_everywhere
    ):
        environment, env_context = _settings_env_context(
            application, env_project, "staging"
        )

        assert environment.slug == "staging"
        assert env_context is enrolled_everywhere["staging"]

    def test_arriving_without_an_environment_uses_the_default(
        self, db_session, application, env_project, enrolled_everywhere
    ):
        environment, env_context = _settings_env_context(application, env_project, None)

        assert environment.slug == "production"
        assert env_context is None

    def test_unknown_environment_degrades_instead_of_aborting(
        self, db_session, application, env_project, enrolled_everywhere
    ):
        environment, env_context = _settings_env_context(
            application, env_project, "deleted-env"
        )

        assert environment.slug == "production"
        assert env_context is None

    def test_environment_the_app_is_not_enrolled_in_is_ignored(
        self, db_session, application, env_project, prod_and_staging, make_app_env
    ):
        prod, _staging = prod_and_staging
        make_app_env(application, prod)

        environment, env_context = _settings_env_context(
            application, env_project, "staging"
        )

        assert environment.slug == "production"
        assert env_context is None


class TestDeleteIsOnlyOfferedAppWide:
    def _render_context(self, app, application, org, environment, env_context):
        with app.test_request_context("/"):
            with patch("cabotage.server.user.views.render_template") as render_template:
                _render_application_settings(
                    application, org, None, environment, env_context
                )
        return render_template.call_args.kwargs

    def test_withheld_when_viewing_from_an_environment(
        self, app, db_session, org, application, env_project, enrolled_everywhere
    ):
        environment, env_context = _settings_env_context(
            application, env_project, "staging"
        )

        ctx = self._render_context(app, application, org, environment, env_context)

        assert ctx["delete_form"] is None
        assert ctx["delete_impact"] is None
        assert ctx["env_context"] is env_context

    def test_offered_on_the_app_wide_page(
        self, app, db_session, org, application, env_project, enrolled_everywhere
    ):
        environment, env_context = _settings_env_context(application, env_project, None)

        ctx = self._render_context(app, application, org, environment, env_context)

        assert ctx["delete_form"] is not None
        assert ctx["env_context"] is None


class TestAppEnvForEnvSlug:
    """Display-only hint: resolution degrades, never aborts."""

    def test_resolves_a_known_slug(
        self, db_session, application, environment, make_app_env
    ):
        app_env = make_app_env(application, environment)

        result = _app_env_for_env_slug(application, "test")

        assert result is not None
        assert result.id == app_env.id

    def test_unknown_slug_returns_none(
        self, db_session, application, environment, make_app_env
    ):
        make_app_env(application, environment)

        assert _app_env_for_env_slug(application, "no-such-env") is None

    @pytest.mark.parametrize("slug", [None, ""])
    def test_missing_slug_returns_none(
        self, db_session, application, environment, make_app_env, slug
    ):
        make_app_env(application, environment)

        assert _app_env_for_env_slug(application, slug) is None

    def test_ignores_soft_deleted_enrolment(
        self, db_session, application, environment, make_app_env
    ):
        import datetime

        make_app_env(
            application,
            environment,
            deleted_at=datetime.datetime.now(datetime.timezone.utc),
        )

        assert _app_env_for_env_slug(application, "test") is None


class TestOverriddenSettings:
    def test_no_overrides_reports_nothing(
        self, db_session, application, environment, make_app_env
    ):
        app_env = make_app_env(application, environment)

        assert app_env.overridden_settings == []

    def test_reports_only_the_overridden_labels(
        self, db_session, application, environment, make_app_env
    ):
        app_env = make_app_env(
            application,
            environment,
            auto_deploy_branch="feature-x",
            deployment_timeout=600,
        )

        assert app_env.overridden_settings == ["Branch", "Deploy Timeout"]

    def test_reports_every_overridable_column(
        self, db_session, application, environment, make_app_env
    ):
        app_env = make_app_env(
            application,
            environment,
            auto_deploy_branch="feature-x",
            github_environment_name="test-env",
            deployment_timeout=600,
            health_check_path="/healthz",
            health_check_host="example.com",
        )

        assert app_env.overridden_settings == [
            label for _, label in ApplicationEnvironment.OVERRIDABLE_SETTINGS
        ]

    def test_labels_stay_in_step_with_the_effective_properties(self):
        """An override nothing reads would be a lie on the page."""
        for attr, _ in ApplicationEnvironment.OVERRIDABLE_SETTINGS:
            assert hasattr(ApplicationEnvironment, f"effective_{attr}")

    def test_branch_override_means_the_app_value_is_not_used(
        self, db_session, application, environment, make_app_env
    ):
        app_env = make_app_env(application, environment, auto_deploy_branch="feature-x")

        assert "Branch" in app_env.overridden_settings
        assert app_env.effective_auto_deploy_branch == "feature-x"
        assert application.auto_deploy_branch == "main"
