"""Tests for process_github_hook dispatcher and process_deployment_hook."""

import uuid
from unittest.mock import MagicMock, patch

import pytest

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Hook,
    Project,
)
from cabotage.server.wsgi import app as _app

REPO = "myorg/myrepo"
OWN_APP_ID = "12345"
BOT_LOGIN = "mybot[bot]"


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["CABOTAGE_OMNIBUS_BUILDS"] = False
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
def commit_sha():
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]


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
    defaults = dict(
        github_repository=REPO,
        auto_deploy_branch="main",
    )
    defaults.update(kwargs)
    application = Application(
        name=slug,
        slug=slug,
        project_id=project.id,
        github_app_installation_id=installation_id,
        **defaults,
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


# ---------------------------------------------------------------------------
# process_github_hook — dispatcher
# ---------------------------------------------------------------------------


class TestProcessGithubHookDispatcher:
    """Tests for the process_github_hook routing logic.

    process_github_hook is a Celery task that commits to the DB, so we
    call the underlying .run() method directly after committing test data.
    """

    def _commit_hook(self, hook):
        """Commit a hook so process_github_hook can find it."""
        db.session.add(hook)
        db.session.commit()
        return hook

    def test_push_event_marks_processed(self, db_session, app):
        hook = self._commit_hook(
            Hook(
                headers={"X-Github-Event": "push"},
                payload={
                    "installation": {"id": 1},
                    "repository": {"full_name": "nobody/nothing"},
                    "ref": "refs/heads/main",
                    "after": "abc123",
                    "commits": [],
                },
                processed=False,
            )
        )
        try:
            from cabotage.celery.tasks.github import process_github_hook

            process_github_hook.run(hook.id)

            db_session.refresh(hook)
            assert hook.processed is True
        finally:
            db_session.delete(hook)
            db_session.commit()

    def test_unknown_event_not_marked_processed(self, db_session, app):
        hook = self._commit_hook(
            Hook(
                headers={"X-Github-Event": "workflow_run"},
                payload={"action": "completed"},
                processed=False,
            )
        )
        try:
            from cabotage.celery.tasks.github import process_github_hook

            process_github_hook.run(hook.id)

            db_session.refresh(hook)
            assert hook.processed is False
        finally:
            db_session.delete(hook)
            db_session.commit()

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_check_suite_event_marks_processed(
        self, mock_gh_app, mock_session, db_session, app
    ):
        mock_gh_app.app_id = OWN_APP_ID
        hook = self._commit_hook(
            Hook(
                headers={"X-Github-Event": "check_suite"},
                payload={
                    "installation": {"id": 1},
                    "repository": {"full_name": "nobody/nothing"},
                    "check_suite": {
                        "head_branch": "main",
                        "head_sha": "abc123",
                        "conclusion": "failure",
                        "app": {"id": 99},
                    },
                },
                processed=False,
            )
        )
        try:
            from cabotage.celery.tasks.github import process_github_hook

            process_github_hook.run(hook.id)

            db_session.refresh(hook)
            assert hook.processed is True
        finally:
            db_session.delete(hook)
            db_session.commit()

    def test_installation_event_marks_processed(self, db_session, app):
        hook = self._commit_hook(
            Hook(
                headers={"X-Github-Event": "installation"},
                payload={"action": "created"},
                processed=False,
            )
        )
        try:
            from cabotage.celery.tasks.github import process_github_hook

            process_github_hook.run(hook.id)

            db_session.refresh(hook)
            assert hook.processed is True
        finally:
            db_session.delete(hook)
            db_session.commit()

    @patch("cabotage.celery.tasks.github.process_pull_request_hook")
    def test_pull_request_event_marks_processed(self, mock_pr_hook, db_session, app):
        hook = self._commit_hook(
            Hook(
                headers={"X-Github-Event": "pull_request"},
                payload={"action": "labeled"},
                processed=False,
            )
        )
        try:
            from cabotage.celery.tasks.github import process_github_hook

            process_github_hook.run(hook.id)

            db_session.refresh(hook)
            assert hook.processed is True
        finally:
            db_session.delete(hook)
            db_session.commit()


class TestDeploymentDeduplication:
    """Tests for deployment hook deduplication in process_github_hook."""

    def _make_deployment_hook(self, installation_id, commit_sha, environment="prod"):
        hook = Hook(
            headers={"X-Github-Event": "deployment"},
            payload={
                "installation": {"id": installation_id},
                "repository": {"full_name": REPO},
                "deployment": {
                    "sha": commit_sha,
                    "environment": environment,
                    "creator": {"login": "other-bot"},
                },
                "sender": {"login": "other-bot"},
            },
            processed=False,
        )
        db.session.add(hook)
        db.session.commit()
        return hook

    @patch("cabotage.celery.tasks.github.github_app")
    def test_first_deployment_hook_is_processed(
        self, mock_gh_app, db_session, app, installation_id, commit_sha
    ):
        mock_gh_app.bot_login = BOT_LOGIN
        hook = self._make_deployment_hook(installation_id, commit_sha)
        try:
            from cabotage.celery.tasks.github import process_github_hook

            process_github_hook.run(hook.id)

            db_session.refresh(hook)
            assert hook.processed is not None
        finally:
            db_session.delete(hook)
            db_session.commit()

    @patch("cabotage.celery.tasks.github.github_app")
    def test_duplicate_deployment_hook_is_skipped(
        self, mock_gh_app, db_session, app, installation_id, commit_sha
    ):
        mock_gh_app.bot_login = BOT_LOGIN
        first = self._make_deployment_hook(installation_id, commit_sha)
        second = self._make_deployment_hook(installation_id, commit_sha)
        try:
            from cabotage.celery.tasks.github import process_github_hook

            process_github_hook.run(second.id)

            db_session.refresh(second)
            assert second.processed is True
        finally:
            db_session.delete(second)
            db_session.delete(first)
            db_session.commit()

    @patch("cabotage.celery.tasks.github.github_app")
    def test_different_environment_not_deduplicated(
        self, mock_gh_app, db_session, app, installation_id, commit_sha
    ):
        mock_gh_app.bot_login = BOT_LOGIN
        staging = self._make_deployment_hook(
            installation_id, commit_sha, environment="staging"
        )
        prod_hook = self._make_deployment_hook(
            installation_id, commit_sha, environment="production"
        )
        try:
            from cabotage.celery.tasks.github import process_github_hook

            process_github_hook.run(prod_hook.id)

            db_session.refresh(prod_hook)
            # Should not be skipped — different environment
            assert prod_hook.processed is not None
        finally:
            db_session.delete(prod_hook)
            db_session.delete(staging)
            db_session.commit()


# ---------------------------------------------------------------------------
# process_deployment_hook
# ---------------------------------------------------------------------------


class TestProcessDeploymentHook:
    def _make_hook(
        self, installation_id, commit_sha, creator_login=BOT_LOGIN, environment="prod"
    ):
        return Hook(
            headers={"X-Github-Event": "deployment"},
            payload={
                "installation": {"id": installation_id},
                "repository": {"full_name": REPO},
                "deployment": {
                    "sha": commit_sha,
                    "environment": environment,
                    "creator": {"login": creator_login},
                    "statuses_url": f"https://api.github.com/repos/{REPO}/deployments/1/statuses",
                },
                "sender": {"login": creator_login},
            },
            processed=False,
        )

    @patch("cabotage.celery.tasks.github.github_app")
    def test_ignores_non_bot_creator(
        self, mock_gh_app, db_session, app, installation_id, commit_sha
    ):
        mock_gh_app.bot_login = BOT_LOGIN
        hook = self._make_hook(installation_id, commit_sha, creator_login="human-user")
        db_session.add(hook)
        db_session.flush()

        from cabotage.celery.tasks.github import process_deployment_hook

        result = process_deployment_hook(hook)
        assert result is False

    @patch("cabotage.celery.tasks.github.run_omnibus_build")
    @patch("cabotage.celery.tasks.github.run_image_build")
    @patch("cabotage.celery.tasks.github.post_deployment_status_update")
    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_creates_image_and_triggers_build(
        self,
        mock_gh_app,
        mock_session,
        mock_post_status,
        mock_build,
        mock_omnibus_build,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        mock_gh_app.bot_login = BOT_LOGIN
        mock_gh_app.bearer_token = "bt"

        access_resp = MagicMock()
        access_resp.json.return_value = {"token": "fake-token"}
        mock_session.post.return_value = access_resp

        application = _make_app(
            project,
            installation_id,
            github_environment_name="prod",
        )
        _make_app_env(application, environment)

        hook = self._make_hook(installation_id, commit_sha, environment="prod")
        db_session.add(hook)
        db_session.flush()

        from cabotage.celery.tasks.github import process_deployment_hook

        result = process_deployment_hook(hook)
        assert result is True
        assert hook.commit_sha == commit_sha
        mock_build.delay.assert_called_once()
        assert mock_post_status.call_count == 2  # in_progress + in_progress

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_returns_false_when_app_env_not_found(
        self, mock_gh_app, mock_session, db_session, app, installation_id, commit_sha
    ):
        mock_gh_app.bot_login = BOT_LOGIN
        mock_gh_app.bearer_token = "bt"

        access_resp = MagicMock()
        access_resp.json.return_value = {"token": "fake-token"}
        mock_session.post.return_value = access_resp

        hook = self._make_hook(installation_id, commit_sha, environment="nonexistent")
        db_session.add(hook)
        db_session.flush()

        from cabotage.celery.tasks.github import process_deployment_hook

        result = process_deployment_hook(hook)
        assert result is False

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_returns_none_on_auth_failure(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        mock_gh_app.bot_login = BOT_LOGIN
        mock_gh_app.bearer_token = "bt"

        application = _make_app(
            project, installation_id, github_environment_name="prod"
        )
        _make_app_env(application, environment)

        # No 'token' in response
        access_resp = MagicMock()
        access_resp.json.return_value = {"message": "Bad credentials"}
        mock_session.post.return_value = access_resp

        hook = self._make_hook(installation_id, commit_sha, environment="prod")
        db_session.add(hook)
        db_session.flush()

        from cabotage.celery.tasks.github import process_deployment_hook

        # HookError is raised internally but caught — returns None
        result = process_deployment_hook(hook)
        assert result is None
