"""Tests for the environment hint on application-wide settings (issue #376).

Application settings are app-wide, but the page keeps the environment the user
navigated from. Two pieces make that safe: a lenient resolver that never aborts
on a stale slug, and the model reporting which settings the environment
overrides so the page can say the value being edited won't apply there.
"""

import uuid

import pytest

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Project,
)
from cabotage.server.user.views import _app_env_for_env_slug
from cabotage.server.wsgi import app as _app


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    with _app.app_context():
        yield _app


@pytest.fixture
def db_session(app):
    yield db.session
    db.session.rollback()


@pytest.fixture
def org(db_session):
    o = Organization(name="Test Org", slug=f"testorg-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    db_session.flush()
    return o


@pytest.fixture
def project(db_session, org):
    p = Project(name="Test Project", organization_id=org.id)
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture
def environment(db_session, project):
    e = Environment(name="test", slug="test", project_id=project.id, ephemeral=False)
    db_session.add(e)
    db_session.flush()
    return e


@pytest.fixture
def application(db_session, project):
    a = Application(
        name="webapp",
        slug="webapp",
        project_id=project.id,
        auto_deploy_branch="main",
    )
    db_session.add(a)
    db_session.flush()
    return a


def _make_app_env(application, environment, **kwargs):
    app_env = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
        **kwargs,
    )
    db.session.add(app_env)
    db.session.flush()
    return app_env


class TestAppEnvForEnvSlug:
    """The hint is display-only, so resolution must degrade, never abort."""

    def test_resolves_a_known_slug(self, db_session, application, environment):
        app_env = _make_app_env(application, environment)

        result = _app_env_for_env_slug(application, "test")

        assert result is not None
        assert result.id == app_env.id

    def test_unknown_slug_returns_none(self, db_session, application, environment):
        _make_app_env(application, environment)

        # A stale bookmark or a deleted environment must not 404 the settings
        # page — that is the page you would go to in order to fix things.
        assert _app_env_for_env_slug(application, "no-such-env") is None

    @pytest.mark.parametrize("slug", [None, ""])
    def test_missing_slug_returns_none(
        self, db_session, application, environment, slug
    ):
        _make_app_env(application, environment)

        assert _app_env_for_env_slug(application, slug) is None

    def test_ignores_soft_deleted_enrollment(
        self, db_session, application, environment
    ):
        import datetime

        _make_app_env(
            application,
            environment,
            deleted_at=datetime.datetime.now(datetime.timezone.utc),
        )

        assert _app_env_for_env_slug(application, "test") is None


class TestOverriddenSettings:
    """What the app-wide page reports as diverging for this environment."""

    def test_no_overrides_reports_nothing(self, db_session, application, environment):
        app_env = _make_app_env(application, environment)

        assert app_env.overridden_settings == []

    def test_reports_only_the_overridden_labels(
        self, db_session, application, environment
    ):
        app_env = _make_app_env(
            application,
            environment,
            auto_deploy_branch="feature-x",
            deployment_timeout=600,
        )

        assert app_env.overridden_settings == ["Branch", "Deploy Timeout"]

    def test_reports_every_overridable_column(
        self, db_session, application, environment
    ):
        app_env = _make_app_env(
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
        """Every overridable column needs an effective_* counterpart, or the
        page would claim an override that nothing actually reads."""
        for attr, _ in ApplicationEnvironment.OVERRIDABLE_SETTINGS:
            assert hasattr(ApplicationEnvironment, f"effective_{attr}")

    def test_branch_override_means_the_app_value_is_not_used(
        self, db_session, application, environment
    ):
        app_env = _make_app_env(
            application, environment, auto_deploy_branch="feature-x"
        )

        assert "Branch" in app_env.overridden_settings
        assert app_env.effective_auto_deploy_branch == "feature-x"
        assert application.auto_deploy_branch == "main"
