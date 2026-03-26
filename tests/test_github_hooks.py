"""Tests for GitHub webhook processing (push + check_suite hooks)."""

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
COMMIT_SHA = "abc123deadbeef"
BRANCH = "main"
OWN_APP_ID = "12345"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    with _app.app_context():
        yield _app


@pytest.fixture
def db_session(app):
    """Provide a transactional scope: rollback everything after each test."""
    yield db.session
    db.session.rollback()


@pytest.fixture
def installation_id():
    """Unique installation ID per test to avoid cross-test collisions."""
    return int(uuid.uuid4().int % 2**31)


@pytest.fixture
def commit_sha():
    """Unique commit SHA per test."""
    return uuid.uuid4().hex[:40]


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


def _make_app(project, installation_id, slug="webapp", watch_paths=None):
    application = Application(
        name=slug,
        slug=slug,
        project_id=project.id,
        github_app_installation_id=installation_id,
        github_repository=REPO,
        auto_deploy_branch=BRANCH,
        branch_deploy_watch_paths=watch_paths,
    )
    db.session.add(application)
    db.session.flush()
    return application


def _make_app_env(application, environment, wait_for_ci=True):
    app_env = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
        auto_deploy_wait_for_ci=wait_for_ci,
    )
    db.session.add(app_env)
    db.session.flush()
    return app_env


def _make_push_hook(
    installation_id, commit_sha=COMMIT_SHA, commits=None, deployed=None
):
    if commits is None:
        commits = [{"added": ["app.py"], "modified": [], "removed": []}]
    hook = Hook(
        headers={"X-Github-Event": "push"},
        payload={
            "installation": {"id": installation_id},
            "repository": {"full_name": REPO},
            "ref": f"refs/heads/{BRANCH}",
            "after": commit_sha,
            "commits": commits,
        },
        processed=False,
        deployed=deployed,
        commit_sha=commit_sha,
    )
    db.session.add(hook)
    db.session.flush()
    return hook


def _make_check_suite_hook(
    installation_id,
    commit_sha,
    conclusion="success",
    suite_app_id=None,
):
    hook = Hook(
        headers={"X-Github-Event": "check_suite"},
        payload={
            "installation": {"id": installation_id},
            "repository": {"full_name": REPO},
            "check_suite": {
                "head_branch": BRANCH,
                "head_sha": commit_sha,
                "conclusion": conclusion,
                "app": {"id": suite_app_id or 99},
            },
        },
        processed=False,
    )
    db.session.add(hook)
    db.session.flush()
    return hook


def _mock_github_responses(
    required_checks=None,
    check_runs=None,
    statuses=None,
):
    if required_checks is None:
        required_checks = []
    if check_runs is None:
        check_runs = []
    if statuses is None:
        statuses = []

    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.text = "{}"

        if "/protection/required_status_checks" in url:
            resp.json.return_value = {
                "checks": required_checks,
                "contexts": [c["context"] for c in required_checks],
            }
        elif "/check-runs" in url:
            resp.json.return_value = {"check_runs": check_runs}
        elif "/status" in url:
            resp.json.return_value = {"statuses": statuses}
        else:
            resp.json.return_value = {}
        return resp

    def mock_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 201
        resp.raise_for_status = MagicMock()

        if "/access_tokens" in url:
            resp.json.return_value = {"token": "fake-token"}
        elif "/deployments" in url:
            resp.json.return_value = {
                "statuses_url": "https://api.github.com/repos/myorg/myrepo/deployments/1/statuses"
            }
        else:
            resp.json.return_value = {}
        return resp

    return mock_get, mock_post


def _count_deployment_posts(mock_session):
    """Count POST calls to the deployments endpoint (excluding access_tokens and statuses)."""
    return len(
        [
            c
            for c in mock_session.post.call_args_list
            if "/deployments" in str(c)
            and "/access_tokens" not in str(c)
            and "/statuses" not in str(c)
        ]
    )


# ---------------------------------------------------------------------------
# _required_contexts_for_branch
# ---------------------------------------------------------------------------


class TestRequiredContextsForBranch:
    @patch("cabotage.celery.tasks.github.github_app")
    @patch("cabotage.celery.tasks.github.github_session")
    def test_filters_own_app_checks(self, mock_session, mock_gh_app, app):
        mock_gh_app.app_id = OWN_APP_ID
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "{}"
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "checks": [
                {"context": "Tests", "app_id": 15368},
                {"context": "Lint", "app_id": 15368},
                {"context": "cabotage-deploy", "app_id": int(OWN_APP_ID)},
            ],
            "contexts": ["Tests", "Lint", "cabotage-deploy"],
        }
        mock_session.get.return_value = resp

        from cabotage.celery.tasks.github import _required_contexts_for_branch

        result = _required_contexts_for_branch({"token": "t"}, REPO, BRANCH)
        assert result == ["Tests", "Lint"]

    @patch("cabotage.celery.tasks.github.github_app")
    @patch("cabotage.celery.tasks.github.github_session")
    def test_returns_empty_on_404(self, mock_session, mock_gh_app, app):
        mock_gh_app.app_id = OWN_APP_ID
        resp = MagicMock()
        resp.status_code = 404
        resp.text = "Not Found"
        mock_session.get.return_value = resp

        from cabotage.celery.tasks.github import _required_contexts_for_branch

        result = _required_contexts_for_branch({"token": "t"}, REPO, BRANCH)
        assert result == []

    @patch("cabotage.celery.tasks.github.github_app")
    @patch("cabotage.celery.tasks.github.github_session")
    def test_falls_back_to_legacy_contexts(self, mock_session, mock_gh_app, app):
        mock_gh_app.app_id = OWN_APP_ID
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "{}"
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "checks": [],
            "contexts": ["ci/tests", "ci/lint"],
        }
        mock_session.get.return_value = resp

        from cabotage.celery.tasks.github import _required_contexts_for_branch

        result = _required_contexts_for_branch({"token": "t"}, REPO, BRANCH)
        assert result == ["ci/tests", "ci/lint"]


# ---------------------------------------------------------------------------
# _all_required_checks_passed
# ---------------------------------------------------------------------------


class TestAllRequiredChecksPassed:
    @patch("cabotage.celery.tasks.github.github_session")
    def test_empty_required_contexts_returns_true(self, mock_session, app):
        from cabotage.celery.tasks.github import _all_required_checks_passed

        assert _all_required_checks_passed({"token": "t"}, REPO, COMMIT_SHA, []) is True
        mock_session.get.assert_not_called()

    @patch("cabotage.celery.tasks.github.github_session")
    def test_all_check_runs_passed(self, mock_session, app):
        check_runs_resp = MagicMock()
        check_runs_resp.raise_for_status = MagicMock()
        check_runs_resp.json.return_value = {
            "check_runs": [
                {"name": "Tests", "conclusion": "success"},
                {"name": "Lint", "conclusion": "success"},
            ]
        }
        status_resp = MagicMock()
        status_resp.raise_for_status = MagicMock()
        status_resp.json.return_value = {"statuses": []}
        mock_session.get.side_effect = [check_runs_resp, status_resp]

        from cabotage.celery.tasks.github import _all_required_checks_passed

        assert (
            _all_required_checks_passed(
                {"token": "t"}, REPO, COMMIT_SHA, ["Tests", "Lint"]
            )
            is True
        )

    @patch("cabotage.celery.tasks.github.github_session")
    def test_failed_check_run_returns_false(self, mock_session, app):
        check_runs_resp = MagicMock()
        check_runs_resp.raise_for_status = MagicMock()
        check_runs_resp.json.return_value = {
            "check_runs": [
                {"name": "Tests", "conclusion": "success"},
                {"name": "Lint", "conclusion": "failure"},
            ]
        }
        status_resp = MagicMock()
        status_resp.raise_for_status = MagicMock()
        status_resp.json.return_value = {"statuses": []}
        mock_session.get.side_effect = [check_runs_resp, status_resp]

        from cabotage.celery.tasks.github import _all_required_checks_passed

        assert (
            _all_required_checks_passed(
                {"token": "t"}, REPO, COMMIT_SHA, ["Tests", "Lint"]
            )
            is False
        )

    @patch("cabotage.celery.tasks.github.github_session")
    def test_pending_check_run_returns_false(self, mock_session, app):
        check_runs_resp = MagicMock()
        check_runs_resp.raise_for_status = MagicMock()
        check_runs_resp.json.return_value = {
            "check_runs": [
                {"name": "Tests", "conclusion": "success"},
                {"name": "Lint", "conclusion": None},
            ]
        }
        status_resp = MagicMock()
        status_resp.raise_for_status = MagicMock()
        status_resp.json.return_value = {"statuses": []}
        mock_session.get.side_effect = [check_runs_resp, status_resp]

        from cabotage.celery.tasks.github import _all_required_checks_passed

        assert (
            _all_required_checks_passed(
                {"token": "t"}, REPO, COMMIT_SHA, ["Tests", "Lint"]
            )
            is False
        )

    @patch("cabotage.celery.tasks.github.github_session")
    def test_mix_of_check_runs_and_commit_statuses(self, mock_session, app):
        check_runs_resp = MagicMock()
        check_runs_resp.raise_for_status = MagicMock()
        check_runs_resp.json.return_value = {
            "check_runs": [{"name": "Tests", "conclusion": "success"}]
        }
        status_resp = MagicMock()
        status_resp.raise_for_status = MagicMock()
        status_resp.json.return_value = {
            "statuses": [{"context": "ci/coverage", "state": "success"}]
        }
        mock_session.get.side_effect = [check_runs_resp, status_resp]

        from cabotage.celery.tasks.github import _all_required_checks_passed

        assert (
            _all_required_checks_passed(
                {"token": "t"}, REPO, COMMIT_SHA, ["Tests", "ci/coverage"]
            )
            is True
        )

    @patch("cabotage.celery.tasks.github.github_session")
    def test_commit_status_pending_returns_false(self, mock_session, app):
        check_runs_resp = MagicMock()
        check_runs_resp.raise_for_status = MagicMock()
        check_runs_resp.json.return_value = {"check_runs": []}
        status_resp = MagicMock()
        status_resp.raise_for_status = MagicMock()
        status_resp.json.return_value = {
            "statuses": [{"context": "ci/coverage", "state": "pending"}]
        }
        mock_session.get.side_effect = [check_runs_resp, status_resp]

        from cabotage.celery.tasks.github import _all_required_checks_passed

        assert (
            _all_required_checks_passed(
                {"token": "t"}, REPO, COMMIT_SHA, ["ci/coverage"]
            )
            is False
        )

    @patch("cabotage.celery.tasks.github.github_session")
    def test_context_absent_returns_false(self, mock_session, app):
        check_runs_resp = MagicMock()
        check_runs_resp.raise_for_status = MagicMock()
        check_runs_resp.json.return_value = {"check_runs": []}
        status_resp = MagicMock()
        status_resp.raise_for_status = MagicMock()
        status_resp.json.return_value = {"statuses": []}
        mock_session.get.side_effect = [check_runs_resp, status_resp]

        from cabotage.celery.tasks.github import _all_required_checks_passed

        assert (
            _all_required_checks_passed({"token": "t"}, REPO, COMMIT_SHA, ["Tests"])
            is False
        )

    @patch("cabotage.celery.tasks.github.github_session")
    def test_paginates_check_runs(self, mock_session, app):
        page1_runs = [
            {"name": f"check-{i}", "conclusion": "success"} for i in range(100)
        ]
        page1_resp = MagicMock()
        page1_resp.raise_for_status = MagicMock()
        page1_resp.json.return_value = {"check_runs": page1_runs}

        page2_resp = MagicMock()
        page2_resp.raise_for_status = MagicMock()
        page2_resp.json.return_value = {
            "check_runs": [{"name": "Final Check", "conclusion": "success"}]
        }

        status_resp = MagicMock()
        status_resp.raise_for_status = MagicMock()
        status_resp.json.return_value = {"statuses": []}

        mock_session.get.side_effect = [page1_resp, page2_resp, status_resp]

        from cabotage.celery.tasks.github import _all_required_checks_passed

        assert (
            _all_required_checks_passed(
                {"token": "t"}, REPO, COMMIT_SHA, ["Final Check"]
            )
            is True
        )


# ---------------------------------------------------------------------------
# process_push_hook — skip CI path
# ---------------------------------------------------------------------------


class TestProcessPushHook:
    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_deploys_immediately_when_wait_for_ci_false(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
    ):
        application = _make_app(project, installation_id, "nowait")
        _make_app_env(application, environment, wait_for_ci=False)
        hook = _make_push_hook(installation_id)

        mock_gh_app.bearer_token = "bt"
        mock_get, mock_post = _mock_github_responses()
        mock_session.get.side_effect = mock_get
        mock_session.post.side_effect = mock_post

        from cabotage.celery.tasks.github import process_push_hook

        process_push_hook(hook)

        assert _count_deployment_posts(mock_session) == 1

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_does_not_deploy_when_wait_for_ci_true(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
    ):
        application = _make_app(project, installation_id, "waitci")
        _make_app_env(application, environment, wait_for_ci=True)
        hook = _make_push_hook(installation_id)

        mock_gh_app.bearer_token = "bt"

        from cabotage.celery.tasks.github import process_push_hook

        process_push_hook(hook)

        assert _count_deployment_posts(mock_session) == 0

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_no_matching_app_returns_false(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
    ):
        application = _make_app(project, installation_id, "otherbranch")
        application.auto_deploy_branch = "develop"
        _make_app_env(application, environment, wait_for_ci=False)
        db.session.flush()
        hook = _make_push_hook(installation_id)

        from cabotage.celery.tasks.github import process_push_hook

        result = process_push_hook(hook)
        assert result is False

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_watch_paths_filters_deployment(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
    ):
        application = _make_app(
            project, installation_id, "watched", watch_paths=["frontend/**"]
        )
        _make_app_env(application, environment, wait_for_ci=False)
        hook = _make_push_hook(
            installation_id,
            commits=[{"added": [], "modified": ["backend/api.py"], "removed": []}],
        )

        mock_gh_app.bearer_token = "bt"
        mock_get, mock_post = _mock_github_responses()
        mock_session.get.side_effect = mock_get
        mock_session.post.side_effect = mock_post

        from cabotage.celery.tasks.github import process_push_hook

        process_push_hook(hook)

        assert _count_deployment_posts(mock_session) == 0

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_mixed_ci_settings_only_deploys_skip_ci(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
    ):
        """With two apps — one skip-CI, one wait-for-CI — push only deploys the skip-CI one."""
        app_skip = _make_app(project, installation_id, "skip")
        _make_app_env(app_skip, environment, wait_for_ci=False)

        app_wait = _make_app(project, installation_id, "wait")
        _make_app_env(app_wait, environment, wait_for_ci=True)

        hook = _make_push_hook(installation_id)

        mock_gh_app.bearer_token = "bt"
        mock_get, mock_post = _mock_github_responses()
        mock_session.get.side_effect = mock_get
        mock_session.post.side_effect = mock_post

        from cabotage.celery.tasks.github import process_push_hook

        process_push_hook(hook)

        assert _count_deployment_posts(mock_session) == 1

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_skip_ci_passes_empty_required_contexts(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
    ):
        """Skip-CI deploys must pass required_contexts=[] so GitHub doesn't
        enforce branch protection checks (which haven't run yet at push time)."""
        application = _make_app(project, installation_id, "skipci2")
        _make_app_env(application, environment, wait_for_ci=False)
        hook = _make_push_hook(installation_id)

        mock_gh_app.bearer_token = "bt"
        mock_get, mock_post = _mock_github_responses()
        mock_session.get.side_effect = mock_get
        mock_session.post.side_effect = mock_post

        from cabotage.celery.tasks.github import process_push_hook

        process_push_hook(hook)

        for c in mock_session.post.call_args_list:
            url = c[0][0] if c[0] else ""
            if (
                "/deployments" in url
                and "/access_tokens" not in url
                and "/statuses" not in url
            ):
                payload = c[1].get("json", {})
                assert payload["required_contexts"] == [], (
                    "skip-CI deployments must send required_contexts=[] "
                    "to bypass branch protection check enforcement"
                )
                break
        else:
            pytest.fail("No deployment POST call found")


# ---------------------------------------------------------------------------
# process_check_suite_hook
# ---------------------------------------------------------------------------


class TestProcessCheckSuiteHook:
    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_ignores_non_success_conclusion(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        _make_app(project, installation_id, "app1")
        hook = _make_check_suite_hook(installation_id, commit_sha, conclusion="failure")

        mock_gh_app.app_id = OWN_APP_ID

        from cabotage.celery.tasks.github import process_check_suite_hook

        process_check_suite_hook(hook)
        mock_session.post.assert_not_called()

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_ignores_own_app_suite(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        _make_app(project, installation_id, "app2")
        hook = _make_check_suite_hook(
            installation_id, commit_sha, suite_app_id=int(OWN_APP_ID)
        )

        mock_gh_app.app_id = OWN_APP_ID

        from cabotage.celery.tasks.github import process_check_suite_hook

        result = process_check_suite_hook(hook)
        assert result is False

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_ignores_when_no_push_event(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        application = _make_app(project, installation_id, "app3")
        _make_app_env(application, environment, wait_for_ci=True)
        # No push hook created — only a check_suite
        hook = _make_check_suite_hook(installation_id, commit_sha)

        mock_gh_app.app_id = OWN_APP_ID

        from cabotage.celery.tasks.github import process_check_suite_hook

        result = process_check_suite_hook(hook)
        assert result is False

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_skips_already_deployed(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        application = _make_app(project, installation_id, "app4")
        _make_app_env(application, environment, wait_for_ci=True)
        _make_push_hook(installation_id, commit_sha=commit_sha, deployed=True)
        hook = _make_check_suite_hook(installation_id, commit_sha)

        mock_gh_app.app_id = OWN_APP_ID
        mock_gh_app.bearer_token = "bt"
        mock_get, mock_post = _mock_github_responses()
        mock_session.get.side_effect = mock_get
        mock_session.post.side_effect = mock_post

        from cabotage.celery.tasks.github import process_check_suite_hook

        result = process_check_suite_hook(hook)
        assert result is False

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_defers_when_checks_not_all_passed(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        """When some required checks haven't passed, don't deploy and don't mark deployed."""
        application = _make_app(project, installation_id, "app5")
        _make_app_env(application, environment, wait_for_ci=True)
        push_hook = _make_push_hook(installation_id, commit_sha=commit_sha)
        hook = _make_check_suite_hook(installation_id, commit_sha)

        mock_gh_app.app_id = OWN_APP_ID
        mock_gh_app.bearer_token = "bt"

        mock_get, mock_post = _mock_github_responses(
            required_checks=[
                {"context": "Tests", "app_id": 15368},
                {"context": "Lint", "app_id": 15368},
            ],
            check_runs=[
                {"name": "Tests", "conclusion": "success"},
                {"name": "Lint", "conclusion": None},  # still running
            ],
        )
        mock_session.get.side_effect = mock_get
        mock_session.post.side_effect = mock_post

        from cabotage.celery.tasks.github import process_check_suite_hook

        result = process_check_suite_hook(hook)
        assert result is False

        # push_event NOT marked deployed so a later suite webhook can retry
        db.session.refresh(push_hook)
        assert push_hook.deployed is not True

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_deploys_when_all_checks_passed(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        application = _make_app(project, installation_id, "app6")
        _make_app_env(application, environment, wait_for_ci=True)
        push_hook = _make_push_hook(installation_id, commit_sha=commit_sha)
        hook = _make_check_suite_hook(installation_id, commit_sha)

        mock_gh_app.app_id = OWN_APP_ID
        mock_gh_app.bearer_token = "bt"

        mock_get, mock_post = _mock_github_responses(
            required_checks=[
                {"context": "Tests", "app_id": 15368},
                {"context": "Lint", "app_id": 15368},
            ],
            check_runs=[
                {"name": "Tests", "conclusion": "success"},
                {"name": "Lint", "conclusion": "success"},
            ],
        )
        mock_session.get.side_effect = mock_get
        mock_session.post.side_effect = mock_post

        from cabotage.celery.tasks.github import process_check_suite_hook

        process_check_suite_hook(hook)

        assert _count_deployment_posts(mock_session) == 1

        db.session.refresh(push_hook)
        assert push_hook.deployed is True

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_deploys_when_no_branch_protection(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        """No branch protection (404) → no required checks → deploys on first suite success."""
        application = _make_app(project, installation_id, "app7")
        _make_app_env(application, environment, wait_for_ci=True)
        _make_push_hook(installation_id, commit_sha=commit_sha)
        hook = _make_check_suite_hook(installation_id, commit_sha)

        mock_gh_app.app_id = OWN_APP_ID
        mock_gh_app.bearer_token = "bt"

        protection_resp = MagicMock()
        protection_resp.status_code = 404
        protection_resp.text = "Not Found"

        def mock_get(url, **kwargs):
            if "/protection/required_status_checks" in url:
                return protection_resp
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"check_runs": []}
            return resp

        _, mock_post = _mock_github_responses()
        mock_session.get.side_effect = mock_get
        mock_session.post.side_effect = mock_post

        from cabotage.celery.tasks.github import process_check_suite_hook

        process_check_suite_hook(hook)

        assert _count_deployment_posts(mock_session) == 1

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_skips_wait_for_ci_false_apps(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        """Apps with wait_for_ci=False are skipped in check_suite (they deploy on push)."""
        application = _make_app(project, installation_id, "app8")
        _make_app_env(application, environment, wait_for_ci=False)
        _make_push_hook(installation_id, commit_sha=commit_sha)
        hook = _make_check_suite_hook(installation_id, commit_sha)

        mock_gh_app.app_id = OWN_APP_ID
        mock_gh_app.bearer_token = "bt"

        mock_get, mock_post = _mock_github_responses()
        mock_session.get.side_effect = mock_get
        mock_session.post.side_effect = mock_post

        from cabotage.celery.tasks.github import process_check_suite_hook

        process_check_suite_hook(hook)

        assert _count_deployment_posts(mock_session) == 0

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_multiple_app_envs_each_get_deployed(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        app1 = _make_app(project, installation_id, "multi1")
        _make_app_env(app1, environment, wait_for_ci=True)

        env2 = Environment(name="staging", project_id=project.id, ephemeral=False)
        db.session.add(env2)
        db.session.flush()

        app2 = _make_app(project, installation_id, "multi2")
        _make_app_env(app2, env2, wait_for_ci=True)

        _make_push_hook(installation_id, commit_sha=commit_sha)
        hook = _make_check_suite_hook(installation_id, commit_sha)

        mock_gh_app.app_id = OWN_APP_ID
        mock_gh_app.bearer_token = "bt"

        mock_get, mock_post = _mock_github_responses(
            required_checks=[{"context": "Tests", "app_id": 15368}],
            check_runs=[{"name": "Tests", "conclusion": "success"}],
        )
        mock_session.get.side_effect = mock_get
        mock_session.post.side_effect = mock_post

        from cabotage.celery.tasks.github import process_check_suite_hook

        process_check_suite_hook(hook)

        assert _count_deployment_posts(mock_session) == 2

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_watch_paths_filters_in_check_suite(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        application = _make_app(
            project, installation_id, "app9", watch_paths=["frontend/**"]
        )
        _make_app_env(application, environment, wait_for_ci=True)
        _make_push_hook(
            installation_id,
            commit_sha=commit_sha,
            commits=[{"added": [], "modified": ["backend/api.py"], "removed": []}],
        )
        hook = _make_check_suite_hook(installation_id, commit_sha)

        mock_gh_app.app_id = OWN_APP_ID
        mock_gh_app.bearer_token = "bt"

        mock_get, mock_post = _mock_github_responses()
        mock_session.get.side_effect = mock_get
        mock_session.post.side_effect = mock_post

        from cabotage.celery.tasks.github import process_check_suite_hook

        process_check_suite_hook(hook)

        assert _count_deployment_posts(mock_session) == 0

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_second_suite_succeeds_after_first_deferred(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        """First check_suite fires with incomplete checks → deferred.
        Second check_suite fires with all checks passed → deploys."""
        application = _make_app(project, installation_id, "app10")
        _make_app_env(application, environment, wait_for_ci=True)
        push_hook = _make_push_hook(installation_id, commit_sha=commit_sha)

        mock_gh_app.app_id = OWN_APP_ID
        mock_gh_app.bearer_token = "bt"

        from cabotage.celery.tasks.github import process_check_suite_hook

        # Round 1: Lint still running
        hook1 = _make_check_suite_hook(installation_id, commit_sha)
        mock_get1, mock_post1 = _mock_github_responses(
            required_checks=[
                {"context": "Tests", "app_id": 15368},
                {"context": "Lint", "app_id": 15368},
            ],
            check_runs=[
                {"name": "Tests", "conclusion": "success"},
                {"name": "Lint", "conclusion": None},
            ],
        )
        mock_session.get.side_effect = mock_get1
        mock_session.post.side_effect = mock_post1

        result1 = process_check_suite_hook(hook1)
        assert result1 is False
        db.session.refresh(push_hook)
        assert push_hook.deployed is not True

        mock_session.reset_mock()

        # Round 2: all passed
        hook2 = _make_check_suite_hook(installation_id, commit_sha)
        mock_get2, mock_post2 = _mock_github_responses(
            required_checks=[
                {"context": "Tests", "app_id": 15368},
                {"context": "Lint", "app_id": 15368},
            ],
            check_runs=[
                {"name": "Tests", "conclusion": "success"},
                {"name": "Lint", "conclusion": "success"},
            ],
        )
        mock_session.get.side_effect = mock_get2
        mock_session.post.side_effect = mock_post2

        process_check_suite_hook(hook2)

        assert _count_deployment_posts(mock_session) == 1
        db.session.refresh(push_hook)
        assert push_hook.deployed is True

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_passes_required_contexts_in_deployment_payload(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
        commit_sha,
    ):
        application = _make_app(project, installation_id, "app11")
        _make_app_env(application, environment, wait_for_ci=True)
        _make_push_hook(installation_id, commit_sha=commit_sha)
        hook = _make_check_suite_hook(installation_id, commit_sha)

        mock_gh_app.app_id = OWN_APP_ID
        mock_gh_app.bearer_token = "bt"

        mock_get, mock_post = _mock_github_responses(
            required_checks=[
                {"context": "Tests", "app_id": 15368},
                {"context": "Lint", "app_id": 15368},
            ],
            check_runs=[
                {"name": "Tests", "conclusion": "success"},
                {"name": "Lint", "conclusion": "success"},
            ],
        )
        mock_session.get.side_effect = mock_get
        mock_session.post.side_effect = mock_post

        from cabotage.celery.tasks.github import process_check_suite_hook

        process_check_suite_hook(hook)

        for c in mock_session.post.call_args_list:
            url = c[0][0] if c[0] else ""
            if (
                "/deployments" in url
                and "/access_tokens" not in url
                and "/statuses" not in url
            ):
                payload = c[1].get("json", {})
                assert payload["required_contexts"] == ["Tests", "Lint"]
                assert payload["ref"] == commit_sha
                break
        else:
            pytest.fail("No deployment POST call found")


# ---------------------------------------------------------------------------
# create_deployment
# ---------------------------------------------------------------------------


class TestCreateDeployment:
    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_uses_prefetched_required_contexts(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
    ):
        """When required_contexts is passed, should not re-fetch from API."""
        application = _make_app(project, installation_id, "app12")
        app_env = _make_app_env(application, environment)

        mock_gh_app.app_id = OWN_APP_ID

        deploy_resp = MagicMock()
        deploy_resp.raise_for_status = MagicMock()
        deploy_resp.json.return_value = {"statuses_url": "https://api.github.com/s/1"}
        status_resp = MagicMock()
        mock_session.post.side_effect = [deploy_resp, status_resp]

        from cabotage.celery.tasks.github import create_deployment

        create_deployment(
            access_token={"token": "t"},
            application=application,
            repository_name=REPO,
            ref=COMMIT_SHA,
            app_env=app_env,
            branch=BRANCH,
            required_contexts=["Tests", "Lint"],
        )

        mock_session.get.assert_not_called()
        deploy_call = mock_session.post.call_args_list[0]
        payload = deploy_call[1].get("json", {})
        assert payload["required_contexts"] == ["Tests", "Lint"]

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_fetches_contexts_when_not_provided(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
    ):
        application = _make_app(project, installation_id, "app13")
        app_env = _make_app_env(application, environment)

        mock_gh_app.app_id = OWN_APP_ID

        protection_resp = MagicMock()
        protection_resp.status_code = 200
        protection_resp.text = "{}"
        protection_resp.raise_for_status = MagicMock()
        protection_resp.json.return_value = {
            "checks": [{"context": "Tests", "app_id": 15368}],
            "contexts": ["Tests"],
        }
        mock_session.get.return_value = protection_resp

        deploy_resp = MagicMock()
        deploy_resp.raise_for_status = MagicMock()
        deploy_resp.json.return_value = {"statuses_url": "https://api.github.com/s/1"}
        status_resp = MagicMock()
        mock_session.post.side_effect = [deploy_resp, status_resp]

        from cabotage.celery.tasks.github import create_deployment

        create_deployment(
            access_token={"token": "t"},
            application=application,
            repository_name=REPO,
            ref=COMMIT_SHA,
            app_env=app_env,
            branch=BRANCH,
        )

        assert any(
            "/protection/required_status_checks" in str(c)
            for c in mock_session.get.call_args_list
        )

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_transient_skips_required_contexts(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
    ):
        application = _make_app(project, installation_id, "app14")
        app_env = _make_app_env(application, environment)

        mock_gh_app.app_id = OWN_APP_ID

        deploy_resp = MagicMock()
        deploy_resp.raise_for_status = MagicMock()
        deploy_resp.json.return_value = {"statuses_url": "https://api.github.com/s/1"}
        status_resp = MagicMock()
        mock_session.post.side_effect = [deploy_resp, status_resp]

        from cabotage.celery.tasks.github import create_deployment

        create_deployment(
            access_token={"token": "t"},
            application=application,
            repository_name=REPO,
            ref=COMMIT_SHA,
            app_env=app_env,
            branch=BRANCH,
            transient_environment=True,
        )

        deploy_call = mock_session.post.call_args_list[0]
        payload = deploy_call[1].get("json", {})
        assert payload["required_contexts"] == []
        assert payload["transient_environment"] is True

    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_required_contexts_without_branch(
        self,
        mock_gh_app,
        mock_session,
        db_session,
        project,
        environment,
        installation_id,
    ):
        """When required_contexts is passed without branch, it should still be
        included in the deployment payload (skip-CI path)."""
        application = _make_app(project, installation_id, "app15")
        app_env = _make_app_env(application, environment)

        mock_gh_app.app_id = OWN_APP_ID

        deploy_resp = MagicMock()
        deploy_resp.raise_for_status = MagicMock()
        deploy_resp.json.return_value = {"statuses_url": "https://api.github.com/s/1"}
        status_resp = MagicMock()
        mock_session.post.side_effect = [deploy_resp, status_resp]

        from cabotage.celery.tasks.github import create_deployment

        create_deployment(
            access_token={"token": "t"},
            application=application,
            repository_name=REPO,
            ref=COMMIT_SHA,
            app_env=app_env,
            required_contexts=[],
        )

        deploy_call = mock_session.post.call_args_list[0]
        payload = deploy_call[1].get("json", {})
        assert payload["required_contexts"] == []
