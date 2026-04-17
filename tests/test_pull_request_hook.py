"""Tests for process_pull_request_hook — branch deploy lifecycle."""

import datetime
import uuid
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    EnvironmentConfiguration,
    Hook,
    Project,
)
from cabotage.server.models.resources import PostgresResource, RedisResource
from cabotage.server.models.utils import safe_k8s_name
from cabotage.server.wsgi import app as _app

REPO = "myorg/myrepo"


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["BACKING_SERVICE_POSTGRES_ENABLED"] = True
    _app.config["BACKING_SERVICE_REDIS_ENABLED"] = True
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


class TestBranchDeployNamespaces:
    def test_render_pr_comment_body_skips_deleted_application_environments(
        self,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
        app,
    ):
        active_app = _make_app(branch_deploy_project, installation_id, slug="server")
        deleted_app = _make_app(branch_deploy_project, installation_id, slug="redis")
        deleted_app.deleted_at = datetime.datetime.now(datetime.timezone.utc)
        db.session.add(deleted_app)
        db.session.flush()

        pr_environment = Environment(
            project_id=branch_deploy_project.id,
            name="PR #27",
            slug="pr-27",
            ephemeral=True,
            uses_environment_namespace=True,
            forked_from_environment_id=environment.id,
        )
        db.session.add(pr_environment)
        db.session.flush()

        _make_app_env(
            active_app,
            pr_environment,
            k8s_identifier=pr_environment.k8s_identifier,
        )
        deleted_ae = _make_app_env(
            deleted_app,
            pr_environment,
            k8s_identifier=pr_environment.k8s_identifier,
        )
        deleted_ae.deleted_at = datetime.datetime.now(datetime.timezone.utc)
        db.session.add(deleted_ae)
        db.session.commit()

        from cabotage.celery.tasks.branch_deploy import _render_pr_comment_body

        body = _render_pr_comment_body(pr_environment)

        assert "server" in body
        assert "redis" not in body

    @patch("cabotage.celery.tasks.branch_deploy.update_pr_comment")
    @patch("cabotage.celery.tasks.branch_deploy._build_images_for_app_envs")
    @patch("cabotage.celery.tasks.branch_deploy._precreate_ingresses")
    @patch("cabotage.celery.tasks.resources.reconcile_backing_services")
    def test_create_branch_deploy_enqueues_backing_service_reconcile_when_services_cloned(
        self,
        mock_reconcile_backing_services,
        mock_precreate,
        mock_build_images,
        mock_update_comment,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)
        db.session.add(
            PostgresResource(
                environment_id=environment.id,
                name="auth-db",
                slug="auth-db",
                service_version="18",
                size_class="db.small",
                storage_size=5,
            )
        )
        db.session.commit()

        from cabotage.celery.tasks.branch_deploy import create_branch_deploy

        create_branch_deploy(
            branch_deploy_project,
            pr_number=42,
            head_sha=uuid.uuid4().hex[:40],
            installation_id=installation_id,
            head_ref="feature-branch",
        )

        mock_reconcile_backing_services.delay.assert_called_once_with()
        mock_precreate.assert_called_once()
        mock_build_images.assert_called_once()
        mock_update_comment.assert_called_once()

    @patch("cabotage.celery.tasks.branch_deploy.update_pr_comment")
    @patch("cabotage.celery.tasks.branch_deploy._build_images_for_app_envs")
    @patch("cabotage.celery.tasks.branch_deploy._precreate_ingresses")
    @patch("cabotage.celery.tasks.resources.reconcile_backing_services")
    def test_create_branch_deploy_skips_backing_service_reconcile_when_no_services(
        self,
        mock_reconcile_backing_services,
        mock_precreate,
        mock_build_images,
        mock_update_comment,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        from cabotage.celery.tasks.branch_deploy import create_branch_deploy

        create_branch_deploy(
            branch_deploy_project,
            pr_number=42,
            head_sha=uuid.uuid4().hex[:40],
            installation_id=installation_id,
            head_ref="feature-branch",
        )

        mock_reconcile_backing_services.delay.assert_not_called()
        mock_precreate.assert_called_once()
        mock_build_images.assert_called_once()
        mock_update_comment.assert_called_once()

    @patch("cabotage.celery.tasks.branch_deploy.update_pr_comment")
    @patch("cabotage.celery.tasks.branch_deploy._build_images_for_app_envs")
    @patch("cabotage.celery.tasks.branch_deploy._precreate_ingresses")
    @patch("cabotage.celery.tasks.resources.reconcile_backing_services")
    def test_create_branch_deploy_skips_disabled_backing_services(
        self,
        mock_reconcile_backing_services,
        mock_precreate,
        mock_build_images,
        mock_update_comment,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
        app,
    ):
        app.config["BACKING_SERVICE_POSTGRES_ENABLED"] = False
        app.config["BACKING_SERVICE_REDIS_ENABLED"] = False

        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)
        db.session.add(
            PostgresResource(
                environment_id=environment.id,
                name="auth-db",
                slug="auth-db",
                service_version="18",
                size_class="db.small",
                storage_size=5,
            )
        )
        db.session.add(
            RedisResource(
                environment_id=environment.id,
                name="cache",
                slug="cache",
                service_version="8",
                size_class="cache.small",
                storage_size=1,
            )
        )
        db.session.commit()

        from cabotage.celery.tasks.branch_deploy import create_branch_deploy

        create_branch_deploy(
            branch_deploy_project,
            pr_number=42,
            head_sha=uuid.uuid4().hex[:40],
            installation_id=installation_id,
            head_ref="feature-branch",
        )

        pr_environment = Environment.query.filter_by(
            project_id=branch_deploy_project.id,
            slug="pr-42",
        ).first()
        assert pr_environment is not None
        assert (
            PostgresResource.query.filter_by(
                environment_id=pr_environment.id, slug="auth-db"
            ).first()
            is None
        )
        assert (
            RedisResource.query.filter_by(
                environment_id=pr_environment.id, slug="cache"
            ).first()
            is None
        )

        mock_reconcile_backing_services.delay.assert_not_called()
        mock_precreate.assert_called_once()
        mock_build_images.assert_called_once()
        mock_update_comment.assert_called_once()

    @patch("cabotage.celery.tasks.branch_deploy.update_pr_comment")
    @patch("cabotage.celery.tasks.branch_deploy._build_images_for_app_envs")
    @patch("cabotage.celery.tasks.branch_deploy._precreate_ingresses")
    def test_create_branch_deploy_uses_environment_namespace(
        self,
        mock_precreate,
        mock_build_images,
        mock_update_comment,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        from cabotage.celery.tasks.branch_deploy import create_branch_deploy

        create_branch_deploy(
            branch_deploy_project,
            pr_number=42,
            head_sha=uuid.uuid4().hex[:40],
            installation_id=installation_id,
            head_ref="feature-branch",
        )

        pr_environment = Environment.query.filter_by(
            project_id=branch_deploy_project.id,
            slug="pr-42",
        ).first()
        assert pr_environment is not None
        assert pr_environment.uses_environment_namespace is True
        assert pr_environment.k8s_namespace == safe_k8s_name(
            branch_deploy_project.organization.k8s_identifier,
            pr_environment.k8s_identifier,
        )

        pr_app_env = ApplicationEnvironment.query.filter_by(
            application_id=application.id,
            environment_id=pr_environment.id,
        ).first()
        assert pr_app_env is not None
        assert pr_app_env.k8s_identifier == pr_environment.k8s_identifier

        mock_precreate.assert_called_once_with(pr_environment)
        mock_build_images.assert_called_once()
        mock_update_comment.assert_called_once_with(pr_environment)

    @patch("cabotage.celery.tasks.branch_deploy.update_pr_comment")
    @patch("cabotage.celery.tasks.branch_deploy._build_images_for_app_envs")
    @patch("cabotage.celery.tasks.branch_deploy._precreate_ingresses")
    def test_create_branch_deploy_clones_backing_services_from_base_environment(
        self,
        mock_precreate,
        mock_build_images,
        mock_update_comment,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        base_pg = PostgresResource(
            environment_id=environment.id,
            name="auth-db",
            slug="auth-db",
            service_version="18",
            size_class="db.small",
            storage_size=5,
            ha_enabled=True,
            backup_strategy="streaming",
            postgres_parameters={"shared_buffers": "128MB"},
        )
        base_redis = RedisResource(
            environment_id=environment.id,
            name="cache",
            slug="cache",
            service_version="8",
            size_class="cache.small",
            storage_size=1,
            ha_enabled=True,
            leader_replicas=2,
            follower_replicas=4,
        )
        db.session.add(base_pg)
        db.session.add(base_redis)
        db.session.commit()

        from cabotage.celery.tasks.branch_deploy import create_branch_deploy

        create_branch_deploy(
            branch_deploy_project,
            pr_number=42,
            head_sha=uuid.uuid4().hex[:40],
            installation_id=installation_id,
            head_ref="feature-branch",
        )

        pr_environment = Environment.query.filter_by(
            project_id=branch_deploy_project.id,
            slug="pr-42",
        ).first()
        assert pr_environment is not None

        cloned_pg = PostgresResource.query.filter_by(
            environment_id=pr_environment.id, slug="auth-db"
        ).first()
        assert cloned_pg is not None
        assert cloned_pg.id != base_pg.id
        assert cloned_pg.service_version == "18"
        assert cloned_pg.size_class == "db.small"
        assert cloned_pg.storage_size == 5
        assert cloned_pg.ha_enabled is True
        assert cloned_pg.backup_strategy == "streaming"
        assert cloned_pg.postgres_parameters == {"shared_buffers": "128MB"}
        assert cloned_pg.provisioning_status == "pending"
        assert cloned_pg.connection_info == {}

        cloned_redis = RedisResource.query.filter_by(
            environment_id=pr_environment.id, slug="cache"
        ).first()
        assert cloned_redis is not None
        assert cloned_redis.id != base_redis.id
        assert cloned_redis.service_version == "8"
        assert cloned_redis.size_class == "cache.small"
        assert cloned_redis.storage_size == 1
        assert cloned_redis.ha_enabled is True
        assert cloned_redis.leader_replicas == 2
        assert cloned_redis.follower_replicas == 4
        assert cloned_redis.provisioning_status == "pending"
        assert cloned_redis.connection_info == {}

        mock_precreate.assert_called_once_with(pr_environment)
        mock_build_images.assert_called_once()
        mock_update_comment.assert_called_once_with(pr_environment)

    @patch("cabotage.celery.tasks.branch_deploy.update_pr_comment")
    @patch("cabotage.celery.tasks.branch_deploy._build_images_for_app_envs")
    @patch("cabotage.celery.tasks.branch_deploy._precreate_ingresses")
    def test_create_branch_deploy_skips_resource_managed_env_configs(
        self,
        mock_precreate,
        mock_build_images,
        mock_update_comment,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
    ):
        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        base_redis = RedisResource(
            environment_id=environment.id,
            name="cache",
            slug="cache",
            service_version="8",
            size_class="cache.small",
            storage_size=1,
        )
        db.session.add(base_redis)
        db.session.flush()
        db.session.add(
            EnvironmentConfiguration(
                project_id=branch_deploy_project.id,
                environment_id=environment.id,
                resource_id=base_redis.id,
                name="CACHE_REDIS_URL",
                value="rediss://base",
                secret=True,
                buildtime=False,
            )
        )
        db.session.add(
            EnvironmentConfiguration(
                project_id=branch_deploy_project.id,
                environment_id=environment.id,
                name="SHARED_ENV",
                value="present",
                secret=False,
                buildtime=False,
            )
        )
        db.session.commit()

        from cabotage.celery.tasks.branch_deploy import create_branch_deploy

        create_branch_deploy(
            branch_deploy_project,
            pr_number=42,
            head_sha=uuid.uuid4().hex[:40],
            installation_id=installation_id,
            head_ref="feature-branch",
        )

        pr_environment = Environment.query.filter_by(
            project_id=branch_deploy_project.id,
            slug="pr-42",
        ).first()
        assert pr_environment is not None
        assert (
            EnvironmentConfiguration.query.filter_by(
                environment_id=pr_environment.id,
                name="CACHE_REDIS_URL",
            ).first()
            is None
        )
        shared = EnvironmentConfiguration.query.filter_by(
            environment_id=pr_environment.id,
            name="SHARED_ENV",
        ).first()
        assert shared is not None
        assert shared.resource_id is None

        mock_precreate.assert_called_once_with(pr_environment)
        mock_build_images.assert_called_once()
        mock_update_comment.assert_called_once_with(pr_environment)

    @patch(
        "cabotage.server.ext.kubernetes.Kubernetes.kubernetes_client",
        new_callable=PropertyMock,
    )
    @patch("cabotage.celery.tasks.deploy.ensure_cabotage_ca_configmap")
    @patch("cabotage.celery.tasks.deploy.ensure_ingresses")
    @patch("cabotage.celery.tasks.deploy.ensure_network_policies")
    @patch("kubernetes.client.NetworkingV1Api")
    @patch("kubernetes.client.CoreV1Api")
    def test_precreate_ingresses_uses_pr_namespace(
        self,
        mock_core_api_cls,
        mock_networking_api_cls,
        mock_ensure_network_policies,
        mock_ensure_ingresses,
        mock_ensure_ca_configmap,
        mock_kubernetes_client,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
        app,
    ):
        app.config["KUBERNETES_ENABLED"] = True
        app.config["NETWORK_POLICIES_ENABLED"] = True
        mock_kubernetes_client.return_value = MagicMock()

        application = _make_app(branch_deploy_project, installation_id)
        pr_environment = Environment(
            project_id=branch_deploy_project.id,
            name="PR #42",
            slug="pr-42",
            ephemeral=True,
            uses_environment_namespace=True,
            forked_from_environment_id=environment.id,
        )
        db.session.add(pr_environment)
        db.session.flush()
        _make_app_env(
            application,
            pr_environment,
            k8s_identifier=pr_environment.k8s_identifier,
        )

        mock_core = MagicMock()
        mock_networking = MagicMock()
        mock_core.read_namespace.side_effect = Exception("not found")
        mock_core_api_cls.return_value = mock_core
        mock_networking_api_cls.return_value = mock_networking

        from kubernetes.client.rest import ApiException
        from cabotage.celery.tasks.branch_deploy import _precreate_ingresses

        mock_core.read_namespace.side_effect = ApiException(status=404)

        _precreate_ingresses(pr_environment)

        expected_namespace = safe_k8s_name(
            branch_deploy_project.organization.k8s_identifier,
            pr_environment.k8s_identifier,
        )
        mock_core.read_namespace.assert_called_once_with(expected_namespace)
        create_call = mock_core.create_namespace.call_args[0][0]
        assert create_call.metadata.name == expected_namespace
        mock_ensure_ca_configmap.assert_called_once_with(mock_core, expected_namespace)
        mock_ensure_network_policies.assert_called_once_with(
            mock_networking, expected_namespace
        )
        mock_ensure_ingresses.assert_called_once()

    @patch(
        "cabotage.server.ext.kubernetes.Kubernetes.kubernetes_client",
        new_callable=PropertyMock,
    )
    @patch("cabotage.celery.tasks.branch_deploy._post_teardown_comment")
    @patch("cabotage.celery.tasks.branch_deploy._deactivate_deployment")
    @patch("kubernetes.client.CoreV1Api")
    def test_teardown_branch_deploy_deletes_pr_namespace(
        self,
        mock_core_api_cls,
        mock_deactivate,
        mock_post_comment,
        mock_kubernetes_client,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
        app,
    ):
        app.config["KUBERNETES_ENABLED"] = True
        mock_kubernetes_client.return_value = MagicMock()

        application = _make_app(branch_deploy_project, installation_id)
        pr_environment = Environment(
            project_id=branch_deploy_project.id,
            name="PR #42",
            slug="pr-42",
            ephemeral=True,
            uses_environment_namespace=True,
            forked_from_environment_id=environment.id,
        )
        db.session.add(pr_environment)
        db.session.flush()
        _make_app_env(
            application,
            pr_environment,
            k8s_identifier=pr_environment.k8s_identifier,
        )

        mock_core = MagicMock()
        mock_core.list_namespaced_persistent_volume_claim.return_value.items = []
        mock_core_api_cls.return_value = mock_core

        from cabotage.celery.tasks.branch_deploy import teardown_branch_deploy

        teardown_branch_deploy(branch_deploy_project, 42)

        expected_namespace = safe_k8s_name(
            branch_deploy_project.organization.k8s_identifier,
            pr_environment.k8s_identifier,
        )
        mock_core.delete_namespace.assert_called_once_with(
            expected_namespace, propagation_policy="Foreground"
        )
        mock_deactivate.assert_called_once()
        mock_post_comment.assert_called_once()

    @patch(
        "cabotage.server.ext.kubernetes.Kubernetes.kubernetes_client",
        new_callable=PropertyMock,
    )
    @patch("cabotage.celery.tasks.branch_deploy.update_pr_comment")
    @patch("cabotage.celery.tasks.branch_deploy._build_images_for_app_envs")
    @patch("cabotage.celery.tasks.branch_deploy._precreate_ingresses")
    @patch("cabotage.celery.tasks.branch_deploy._post_teardown_comment")
    @patch("cabotage.celery.tasks.branch_deploy._deactivate_deployment")
    @patch("cabotage.celery.tasks.resources._release_reconcile_lock")
    @patch("cabotage.celery.tasks.resources._acquire_reconcile_lock")
    @patch("kubernetes.client.CoreV1Api")
    def test_teardown_branch_deploy_deletes_cloned_backing_services(
        self,
        mock_core_api_cls,
        mock_acquire_reconcile_lock,
        mock_release_reconcile_lock,
        mock_deactivate,
        mock_post_comment,
        mock_precreate,
        mock_build_images,
        mock_update_comment,
        mock_kubernetes_client,
        db_session,
        branch_deploy_project,
        environment,
        installation_id,
        app,
    ):
        app.config["KUBERNETES_ENABLED"] = True
        mock_kubernetes_client.return_value = MagicMock()
        mock_lock_conn = MagicMock()
        mock_acquire_reconcile_lock.return_value = mock_lock_conn

        application = _make_app(branch_deploy_project, installation_id)
        _make_app_env(application, environment)

        base_pg = PostgresResource(
            environment_id=environment.id,
            name="auth-db",
            slug="auth-db",
            service_version="18",
            size_class="db.small",
            storage_size=5,
            backup_strategy="streaming",
        )
        db.session.add(base_pg)
        db.session.commit()

        from cabotage.celery.tasks.branch_deploy import (
            create_branch_deploy,
            teardown_branch_deploy,
        )

        create_branch_deploy(
            branch_deploy_project,
            pr_number=42,
            head_sha=uuid.uuid4().hex[:40],
            installation_id=installation_id,
            head_ref="feature-branch",
        )

        pr_environment = Environment.query.filter_by(
            project_id=branch_deploy_project.id,
            slug="pr-42",
        ).first()
        assert pr_environment is not None
        cloned_pg = PostgresResource.query.filter_by(
            environment_id=pr_environment.id, slug="auth-db"
        ).first()
        assert cloned_pg is not None

        db.session.expire_all()

        mock_core = MagicMock()
        pvc_one = MagicMock()
        pvc_one.metadata.name = "pgdata-auth-db-0"
        pvc_two = MagicMock()
        pvc_two.metadata.name = "pgdata-auth-db-1"
        mock_core.list_namespaced_persistent_volume_claim.return_value.items = [
            pvc_one,
            pvc_two,
        ]
        mock_core_api_cls.return_value = mock_core
        mock_delete_postgres = MagicMock()

        with patch.dict(
            "cabotage.celery.tasks.resources._RECONCILERS",
            {"postgres": (MagicMock(), mock_delete_postgres)},
            clear=False,
        ):
            teardown_branch_deploy(branch_deploy_project, 42)

        assert Environment.query.filter_by(id=pr_environment.id).first() is None
        assert PostgresResource.query.filter_by(id=cloned_pg.id).first() is None
        assert PostgresResource.query.filter_by(id=base_pg.id).first() is not None
        mock_delete_postgres.assert_called_once()
        mock_acquire_reconcile_lock.assert_called_once_with()
        mock_release_reconcile_lock.assert_called_once_with(mock_lock_conn)
        deleted_resource = mock_delete_postgres.call_args[0][0]
        assert deleted_resource.id == cloned_pg.id
        pvc_calls = [
            call
            for call in mock_core.delete_namespaced_persistent_volume_claim.call_args_list
            if call.args[1] == pr_environment.k8s_namespace
        ]
        assert {call.args[0] for call in pvc_calls} == {
            "pgdata-auth-db-0",
            "pgdata-auth-db-1",
        }


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
