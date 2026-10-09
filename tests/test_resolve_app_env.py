"""Tests for _resolve_app_env_for_hook — pure DB tests, no mocking."""

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
from cabotage.server.wsgi import app as _app

REPO = "myorg/myrepo"


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
def installation_id():
    return int(uuid.uuid4().int % 2**31)


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
    e = Environment(name="default", project_id=project.id, ephemeral=False)
    db_session.add(e)
    db_session.flush()
    return e


def _make_app(project, installation_id, slug="webapp", **kwargs):
    application = Application(
        name=slug,
        slug=slug,
        project_id=project.id,
        github_app_installation_id=installation_id,
        github_repository=REPO,
        **kwargs,
    )
    db.session.add(application)
    db.session.flush()
    return application


def _make_app_env(application, environment, **kwargs):
    app_env = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
        **kwargs,
    )
    db.session.add(app_env)
    db.session.flush()
    return app_env


class TestResolveAppEnvByGithubEnvironmentName:
    """Stage 1: match ApplicationEnvironment.github_environment_name."""

    def test_matches_app_env_github_environment_name(
        self, db_session, project, environment, installation_id
    ):
        application = _make_app(project, installation_id)
        app_env = _make_app_env(
            application, environment, github_environment_name="production"
        )

        from cabotage.celery.tasks.github import _resolve_app_env_for_hook

        result = _resolve_app_env_for_hook(installation_id, REPO, "production")
        assert result is not None
        assert result.id == app_env.id

    def test_no_match_falls_through(
        self, db_session, project, environment, installation_id
    ):
        application = _make_app(project, installation_id)
        _make_app_env(application, environment, github_environment_name="staging")

        from cabotage.celery.tasks.github import _resolve_app_env_for_hook

        result = _resolve_app_env_for_hook(installation_id, REPO, "production")
        assert result is None

    def test_ignores_deleted_app_env(
        self, db_session, project, environment, installation_id
    ):
        application = _make_app(project, installation_id)
        _make_app_env(
            application,
            environment,
            github_environment_name="production",
            deleted_at=db.func.now(),
        )

        from cabotage.celery.tasks.github import _resolve_app_env_for_hook

        result = _resolve_app_env_for_hook(installation_id, REPO, "production")
        assert result is None

    def test_ignores_deleted_application(
        self, db_session, project, environment, installation_id
    ):
        from datetime import datetime, timezone

        application = _make_app(project, installation_id)
        application.deleted_at = datetime.now(timezone.utc)
        db.session.flush()
        _make_app_env(application, environment, github_environment_name="production")

        from cabotage.celery.tasks.github import _resolve_app_env_for_hook

        result = _resolve_app_env_for_hook(installation_id, REPO, "production")
        assert result is None


class TestResolveAppByGithubEnvironmentName:
    """Stage 2: match Application.github_environment_name → default_app_env."""

    def test_matches_app_github_environment_name(
        self, db_session, project, environment, installation_id
    ):
        application = _make_app(
            project, installation_id, github_environment_name="production"
        )
        app_env = _make_app_env(application, environment)

        from cabotage.celery.tasks.github import _resolve_app_env_for_hook

        result = _resolve_app_env_for_hook(installation_id, REPO, "production")
        assert result is not None
        assert result.id == app_env.id

    def test_no_match_on_app_env_name_falls_to_app_name(
        self, db_session, project, environment, installation_id
    ):
        """When no AppEnv has a matching github_environment_name, stage 2
        tries Application.github_environment_name."""
        application = _make_app(
            project, installation_id, slug="app1", github_environment_name="production"
        )
        app_env = _make_app_env(application, environment)

        from cabotage.celery.tasks.github import _resolve_app_env_for_hook

        result = _resolve_app_env_for_hook(installation_id, REPO, "production")
        assert result is not None
        assert result.id == app_env.id


class TestResolveBySlug:
    """Stage 3: slug-based parsing (project/app, project/env/app, org/project/env/app)."""

    def test_two_part_slug(
        self, db_session, org, project, environment, installation_id
    ):
        application = _make_app(project, installation_id)
        app_env = _make_app_env(application, environment)

        from cabotage.celery.tasks.github import _resolve_app_env_for_hook

        env_string = f"{project.slug}/{application.slug}"
        result = _resolve_app_env_for_hook(installation_id, REPO, env_string)
        assert result is not None
        assert result.id == app_env.id

    def test_three_part_slug(
        self, db_session, org, project, environment, installation_id
    ):
        application = _make_app(project, installation_id)
        app_env = _make_app_env(application, environment)

        from cabotage.celery.tasks.github import _resolve_app_env_for_hook

        env_string = f"{project.slug}/{environment.slug}/{application.slug}"
        result = _resolve_app_env_for_hook(installation_id, REPO, env_string)
        assert result is not None
        assert result.id == app_env.id

    def test_four_part_slug(
        self, db_session, org, project, environment, installation_id
    ):
        application = _make_app(project, installation_id)
        app_env = _make_app_env(application, environment)

        from cabotage.celery.tasks.github import _resolve_app_env_for_hook

        env_string = f"{org.slug}/{project.slug}/{environment.slug}/{application.slug}"
        result = _resolve_app_env_for_hook(installation_id, REPO, env_string)
        assert result is not None
        assert result.id == app_env.id

    def test_wrong_slug_returns_none(
        self, db_session, org, project, environment, installation_id
    ):
        application = _make_app(project, installation_id)
        _make_app_env(application, environment)

        from cabotage.celery.tasks.github import _resolve_app_env_for_hook

        result = _resolve_app_env_for_hook(installation_id, REPO, "wrong/slug")
        assert result is None

    def test_wrong_installation_id_returns_none(
        self, db_session, org, project, environment, installation_id
    ):
        application = _make_app(project, installation_id)
        _make_app_env(application, environment)

        from cabotage.celery.tasks.github import _resolve_app_env_for_hook

        env_string = f"{project.slug}/{application.slug}"
        result = _resolve_app_env_for_hook(99999999, REPO, env_string)
        assert result is None

    def test_single_slug_returns_none(
        self, db_session, project, environment, installation_id
    ):
        """A single slug with no '/' doesn't match any pattern."""
        _make_app(project, installation_id)

        from cabotage.celery.tasks.github import _resolve_app_env_for_hook

        result = _resolve_app_env_for_hook(installation_id, REPO, "just-a-name")
        assert result is None


class TestResolvePrecedence:
    """Stage 1 should take priority over stage 2 and 3."""

    def test_app_env_name_wins_over_app_name(
        self, db_session, project, environment, installation_id
    ):
        """When both AppEnv and App have matching github_environment_name,
        the AppEnv match (stage 1) wins."""
        application = _make_app(
            project, installation_id, github_environment_name="production"
        )
        # The app-level match would return this via default_app_env
        _make_app_env(application, environment)

        # But stage 1 has an explicit match on a different app_env
        env2 = Environment(name="staging", project_id=project.id, ephemeral=False)
        db.session.add(env2)
        db.session.flush()
        explicit_app_env = _make_app_env(
            application, env2, github_environment_name="production"
        )

        from cabotage.celery.tasks.github import _resolve_app_env_for_hook

        result = _resolve_app_env_for_hook(installation_id, REPO, "production")
        assert result.id == explicit_app_env.id
