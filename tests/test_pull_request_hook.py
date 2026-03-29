"""Tests for process_pull_request_hook — branch deploy lifecycle."""

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


@pytest.fixture
def branch_deploy_project(db_session, org, environment):
    """A project with branch deploys enabled."""
    p = Project(
        name="BD Project",
        organization_id=org.id,
        branch_deploys_enabled=True,
        branch_deploy_base_environment_id=environment.id,
    )
    db_session.add(p)
    db_session.flush()
    return p


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


def _make_pr_hook(
    installation_id,
    action="opened",
    merged=False,
    head_ref="feature-branch",
    base_ref="main",
    author_login="human",
    author_type="User",
    head_repo=REPO,
    base_repo=REPO,
):
    pr = {
        "number": 42,
        "head": {
            "sha": uuid.uuid4().hex[:40],
            "ref": head_ref,
            "repo": {"full_name": head_repo},
        },
        "base": {
            "ref": base_ref,
            "repo": {"full_name": base_repo},
        },
        "user": {"login": author_login, "type": author_type},
        "merged": merged,
    }
    hook = Hook(
        headers={"X-Github-Event": "pull_request"},
        payload={
            "action": action,
            "installation": {"id": installation_id},
            "repository": {"full_name": REPO},
            "pull_request": pr,
        },
        processed=False,
    )
    db.session.add(hook)
    db.session.flush()
    return hook


# ---------------------------------------------------------------------------
# Action filtering
# ---------------------------------------------------------------------------


class TestPullRequestActionFiltering:
    def test_ignores_labeled_action(self, db_session, app, installation_id):
        hook = _make_pr_hook(installation_id, action="labeled")

        from cabotage.celery.tasks.github import process_pull_request_hook

        result = process_pull_request_hook(hook)
        assert result is None

    def test_ignores_review_requested(self, db_session, app, installation_id):
        hook = _make_pr_hook(installation_id, action="review_requested")

        from cabotage.celery.tasks.github import process_pull_request_hook

        result = process_pull_request_hook(hook)
        assert result is None

    def test_ignores_edited(self, db_session, app, installation_id):
        hook = _make_pr_hook(installation_id, action="edited")

        from cabotage.celery.tasks.github import process_pull_request_hook

        result = process_pull_request_hook(hook)
        assert result is None

    def test_ignores_converted_to_draft(self, db_session, app, installation_id):
        hook = _make_pr_hook(installation_id, action="converted_to_draft")

        from cabotage.celery.tasks.github import process_pull_request_hook

        result = process_pull_request_hook(hook)
        assert result is None


# ---------------------------------------------------------------------------
# Fork and bot filtering
# ---------------------------------------------------------------------------


class TestPullRequestSecurityFiltering:
    def test_ignores_fork_prs(self, db_session, app, installation_id):
        hook = _make_pr_hook(
            installation_id,
            action="opened",
            head_repo="attacker/fork",
            base_repo=REPO,
        )

        from cabotage.celery.tasks.github import process_pull_request_hook

        result = process_pull_request_hook(hook)
        assert result is None
        assert hook.commit_sha is None

    def test_ignores_bot_prs(self, db_session, app, installation_id):
        hook = _make_pr_hook(
            installation_id,
            action="opened",
            author_login="dependabot[bot]",
            author_type="Bot",
        )

        from cabotage.celery.tasks.github import process_pull_request_hook

        result = process_pull_request_hook(hook)
        assert result is None
        assert hook.commit_sha is None

    def test_ignores_bot_login_suffix(self, db_session, app, installation_id):
        hook = _make_pr_hook(
            installation_id,
            action="opened",
            author_login="renovate[bot]",
            author_type="User",  # some bots report as User
        )

        from cabotage.celery.tasks.github import process_pull_request_hook

        result = process_pull_request_hook(hook)
        assert result is None


# ---------------------------------------------------------------------------
# Branch deploy lifecycle
# ---------------------------------------------------------------------------


class TestPullRequestBranchDeploy:
    @patch("cabotage.celery.tasks.github.create_branch_deploy")
    def test_opened_creates_branch_deploy(
        self,
        mock_create,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        hook = _make_pr_hook(installation_id, action="opened", base_ref="main")

        from cabotage.celery.tasks.github import process_pull_request_hook

        process_pull_request_hook(hook)

        mock_create.assert_called_once()
        args = mock_create.call_args
        assert args[0][0] == branch_deploy_project  # project
        assert args[0][1] == 42  # pr_number

    @patch("cabotage.celery.tasks.github.create_branch_deploy")
    def test_reopened_creates_branch_deploy(
        self,
        mock_create,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        hook = _make_pr_hook(installation_id, action="reopened", base_ref="main")

        from cabotage.celery.tasks.github import process_pull_request_hook

        process_pull_request_hook(hook)

        mock_create.assert_called_once()

    @patch("cabotage.celery.tasks.github.sync_branch_deploy")
    def test_synchronize_syncs_branch_deploy(
        self,
        mock_sync,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        hook = _make_pr_hook(installation_id, action="synchronize", base_ref="main")

        from cabotage.celery.tasks.github import process_pull_request_hook

        process_pull_request_hook(hook)

        mock_sync.assert_called_once()
        args = mock_sync.call_args
        assert args[0][0] == branch_deploy_project
        assert args[0][1] == 42

    @patch("cabotage.celery.tasks.github.teardown_branch_deploy")
    def test_closed_tears_down_branch_deploy(
        self,
        mock_teardown,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        hook = _make_pr_hook(
            installation_id, action="closed", merged=True, base_ref="main"
        )

        from cabotage.celery.tasks.github import process_pull_request_hook

        process_pull_request_hook(hook)

        mock_teardown.assert_called_once()
        args = mock_teardown.call_args
        assert args[0][0] == branch_deploy_project
        assert args[0][1] == 42

    @patch("cabotage.celery.tasks.github.create_branch_deploy")
    def test_no_branch_deploy_without_enabled_project(
        self,
        mock_create,
        db_session,
        project,  # regular project, no branch deploys
        environment,
        installation_id,
    ):
        application = _make_app(project, installation_id)
        _make_app_env(application, environment)

        hook = _make_pr_hook(installation_id, action="opened", base_ref="main")

        from cabotage.celery.tasks.github import process_pull_request_hook

        process_pull_request_hook(hook)

        mock_create.assert_not_called()

    @patch("cabotage.celery.tasks.github.create_branch_deploy")
    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_skips_when_base_branch_doesnt_match_auto_deploy(
        self,
        mock_gh_app,
        mock_session,
        mock_create,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        """PR targeting 'develop' should not trigger branch deploy if the app
        auto-deploys from 'main' and no stacked PR chain exists."""
        mock_gh_app.bearer_token = "bt"
        # Access token response
        access_resp = MagicMock()
        access_resp.json.return_value = {"token": "fake-token"}
        mock_session.post.return_value = access_resp
        # No open PRs from 'develop'
        pulls_resp = MagicMock()
        pulls_resp.status_code = 200
        pulls_resp.json.return_value = []
        mock_session.get.return_value = pulls_resp

        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        hook = _make_pr_hook(installation_id, action="opened", base_ref="develop")

        from cabotage.celery.tasks.github import process_pull_request_hook

        process_pull_request_hook(hook)

        mock_create.assert_not_called()

    @patch("cabotage.celery.tasks.github.create_branch_deploy")
    def test_sets_commit_sha_on_hook(
        self,
        mock_create,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        hook = _make_pr_hook(installation_id, action="opened", base_ref="main")

        from cabotage.celery.tasks.github import process_pull_request_hook

        process_pull_request_hook(hook)

        assert hook.commit_sha is not None
        assert len(hook.commit_sha) > 0


# ---------------------------------------------------------------------------
# _base_ref_chains_to_auto_deploy_branch
# ---------------------------------------------------------------------------


class TestBaseRefChainsToAutoDeployBranch:
    """Unit tests for the stacked PR chain-walking function."""

    def _mock_pulls_api(self, chains):
        """Build a mock for github_session.get that returns open PRs.

        Args:
            chains: dict mapping head_ref -> list of base_refs for open PRs.
                    e.g. {"backend": ["main"], "frontend": ["backend"]}
        """

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/pulls" in url:
                head_param = kwargs.get("params", {}).get("head", "")
                # head param is "owner:branch"
                branch = head_param.split(":")[-1] if ":" in head_param else head_param
                prs = [{"base": {"ref": base}} for base in chains.get(branch, [])]
                resp.json.return_value = prs
            else:
                resp.json.return_value = {}
            return resp

        return mock_get

    @patch("cabotage.celery.tasks.github.github_session")
    def test_single_level_stack(self, mock_session, app):
        """backend -> main: checking 'backend' finds the chain."""
        mock_session.get.side_effect = self._mock_pulls_api({"backend": ["main"]})

        from cabotage.celery.tasks.github import _base_ref_chains_to_auto_deploy_branch

        result = _base_ref_chains_to_auto_deploy_branch(
            {"token": "t"}, "myorg/myrepo", "backend", {"main"}
        )
        assert result is True

    @patch("cabotage.celery.tasks.github.github_session")
    def test_two_level_stack(self, mock_session, app):
        """frontend -> backend -> main: checking 'frontend' finds the chain."""
        mock_session.get.side_effect = self._mock_pulls_api(
            {
                "frontend": ["backend"],
                "backend": ["main"],
            }
        )

        from cabotage.celery.tasks.github import _base_ref_chains_to_auto_deploy_branch

        result = _base_ref_chains_to_auto_deploy_branch(
            {"token": "t"}, "myorg/myrepo", "frontend", {"main"}
        )
        assert result is True

    @patch("cabotage.celery.tasks.github.github_session")
    def test_no_open_pr_returns_false(self, mock_session, app):
        """No open PR from 'random-branch' — returns False."""
        mock_session.get.side_effect = self._mock_pulls_api({})

        from cabotage.celery.tasks.github import _base_ref_chains_to_auto_deploy_branch

        result = _base_ref_chains_to_auto_deploy_branch(
            {"token": "t"}, "myorg/myrepo", "random-branch", {"main"}
        )
        assert result is False

    @patch("cabotage.celery.tasks.github.github_session")
    def test_chain_to_wrong_branch_returns_false(self, mock_session, app):
        """backend -> develop, but auto_deploy is 'main' — returns False."""
        mock_session.get.side_effect = self._mock_pulls_api({"backend": ["develop"]})

        from cabotage.celery.tasks.github import _base_ref_chains_to_auto_deploy_branch

        result = _base_ref_chains_to_auto_deploy_branch(
            {"token": "t"}, "myorg/myrepo", "backend", {"main"}
        )
        assert result is False

    @patch("cabotage.celery.tasks.github.github_session")
    def test_cycle_detection(self, mock_session, app):
        """a -> b and b -> a: should not infinite loop."""
        mock_session.get.side_effect = self._mock_pulls_api(
            {
                "branch-a": ["branch-b"],
                "branch-b": ["branch-a"],
            }
        )

        from cabotage.celery.tasks.github import _base_ref_chains_to_auto_deploy_branch

        result = _base_ref_chains_to_auto_deploy_branch(
            {"token": "t"}, "myorg/myrepo", "branch-a", {"main"}
        )
        assert result is False

    @patch("cabotage.celery.tasks.github.github_session")
    def test_api_error_returns_false(self, mock_session, app):
        """API returning non-200 should return False gracefully."""
        resp = MagicMock()
        resp.status_code = 403
        mock_session.get.return_value = resp

        from cabotage.celery.tasks.github import _base_ref_chains_to_auto_deploy_branch

        result = _base_ref_chains_to_auto_deploy_branch(
            {"token": "t"}, "myorg/myrepo", "backend", {"main"}
        )
        assert result is False

    @patch("cabotage.celery.tasks.github.github_session")
    def test_multiple_auto_deploy_branches(self, mock_session, app):
        """Chain reaches one of several auto-deploy branches."""
        mock_session.get.side_effect = self._mock_pulls_api(
            {
                "feature": ["release/v2"],
            }
        )

        from cabotage.celery.tasks.github import _base_ref_chains_to_auto_deploy_branch

        result = _base_ref_chains_to_auto_deploy_branch(
            {"token": "t"}, "myorg/myrepo", "feature", {"main", "release/v2"}
        )
        assert result is True


# ---------------------------------------------------------------------------
# Stacked PR integration tests
# ---------------------------------------------------------------------------


class TestStackedPullRequestBranchDeploy:
    def _mock_access_token(self):
        resp = MagicMock()
        resp.json.return_value = {"token": "fake-token"}
        return resp

    def _mock_pulls_api(self, chains):
        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/pulls" in url:
                head_param = kwargs.get("params", {}).get("head", "")
                branch = head_param.split(":")[-1] if ":" in head_param else head_param
                prs = [{"base": {"ref": base}} for base in chains.get(branch, [])]
                resp.json.return_value = prs
            else:
                resp.json.return_value = {}
            return resp

        return mock_get

    @patch("cabotage.celery.tasks.github.create_branch_deploy")
    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_stacked_pr_opened_creates_branch_deploy(
        self,
        mock_gh_app,
        mock_session,
        mock_create,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        mock_gh_app.bearer_token = "bt"
        mock_session.post.return_value = self._mock_access_token()
        mock_session.get.side_effect = self._mock_pulls_api({"backend": ["main"]})

        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        # PR frontend -> backend (stacked on backend -> main)
        hook = _make_pr_hook(
            installation_id,
            action="opened",
            head_ref="frontend",
            base_ref="backend",
        )

        from cabotage.celery.tasks.github import process_pull_request_hook

        process_pull_request_hook(hook)

        mock_create.assert_called_once()

    @patch("cabotage.celery.tasks.github.teardown_branch_deploy")
    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_stacked_pr_closed_tears_down(
        self,
        mock_gh_app,
        mock_session,
        mock_teardown,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        mock_gh_app.bearer_token = "bt"
        mock_session.post.return_value = self._mock_access_token()
        mock_session.get.side_effect = self._mock_pulls_api({"backend": ["main"]})

        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        hook = _make_pr_hook(
            installation_id,
            action="closed",
            head_ref="frontend",
            base_ref="backend",
        )

        from cabotage.celery.tasks.github import process_pull_request_hook

        process_pull_request_hook(hook)

        mock_teardown.assert_called_once()

    @patch("cabotage.celery.tasks.github.sync_branch_deploy")
    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_stacked_pr_synchronize_syncs(
        self,
        mock_gh_app,
        mock_session,
        mock_sync,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        mock_gh_app.bearer_token = "bt"
        mock_session.post.return_value = self._mock_access_token()
        mock_session.get.side_effect = self._mock_pulls_api({"backend": ["main"]})

        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        hook = _make_pr_hook(
            installation_id,
            action="synchronize",
            head_ref="frontend",
            base_ref="backend",
        )

        from cabotage.celery.tasks.github import process_pull_request_hook

        process_pull_request_hook(hook)

        mock_sync.assert_called_once()

    @patch("cabotage.celery.tasks.github.create_branch_deploy")
    @patch("cabotage.celery.tasks.github.github_session")
    @patch("cabotage.celery.tasks.github.github_app")
    def test_broken_chain_skips(
        self,
        mock_gh_app,
        mock_session,
        mock_create,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        """PR targeting a branch with no open PR to main — should skip."""
        mock_gh_app.bearer_token = "bt"
        mock_session.post.return_value = self._mock_access_token()
        mock_session.get.side_effect = self._mock_pulls_api({})  # no open PRs

        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        hook = _make_pr_hook(
            installation_id,
            action="opened",
            head_ref="frontend",
            base_ref="backend",
        )

        from cabotage.celery.tasks.github import process_pull_request_hook

        process_pull_request_hook(hook)

        mock_create.assert_not_called()

    @patch("cabotage.celery.tasks.github.create_branch_deploy")
    def test_direct_match_skips_api_call(
        self,
        mock_create,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        """PR targeting main directly should NOT call the GitHub API."""
        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        hook = _make_pr_hook(installation_id, action="opened", base_ref="main")

        with patch("cabotage.celery.tasks.github.github_session") as mock_session:
            from cabotage.celery.tasks.github import process_pull_request_hook

            process_pull_request_hook(hook)

            # No GET calls for PR chain checking
            mock_session.get.assert_not_called()
            # But create_branch_deploy should still be called
            mock_create.assert_called_once()
