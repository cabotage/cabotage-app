"""Tests for per-application-environment deploy queuing."""

import datetime
import uuid
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa

from cabotage.celery.tasks.deploy import run_deploy, start_next_deployment
from cabotage.celery.tasks.maintain import reap_stale_builds
from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Deployment,
    Environment,
    Project,
)
from cabotage.server.wsgi import app as _app
from cabotage.utils.build_log_stream import heartbeat_key


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["KUBERNETES_ENABLED"] = True
    _app.config["CELERY_BROKER_URL"] = "redis://localhost:6379/0"
    with _app.app_context():
        yield _app


@pytest.fixture
def db_session(app):
    yield db.session
    db.session.rollback()


@pytest.fixture
def project(db_session):
    org = Organization(name="Test Org", slug=f"testorg-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.flush()
    p = Project(name="Test Project", organization_id=org.id)
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture
def application(db_session, project):
    a = Application(name="webapp", slug="webapp", project_id=project.id)
    db_session.add(a)
    db_session.flush()
    return a


def _app_env(db_session, project, application, name):
    environment = Environment(name=name, project_id=project.id, ephemeral=False)
    db_session.add(environment)
    db_session.flush()
    ae = ApplicationEnvironment(
        application_id=application.id, environment_id=environment.id
    )
    db_session.add(ae)
    db_session.flush()
    return ae


@pytest.fixture
def app_env(db_session, project, application):
    return _app_env(db_session, project, application, "default")


@pytest.fixture
def run_deploy_task():
    with patch("cabotage.celery.tasks.deploy.run_deploy") as mock:
        yield mock.delay


def _deployment(db_session, app_env):
    d = Deployment(
        application_id=app_env.application_id,
        application_environment_id=app_env.id,
        release={},
    )
    db_session.add(d)
    db_session.commit()
    return d


def _trigger(db_session, app_env):
    d = _deployment(db_session, app_env)
    start_next_deployment(app_env.id)
    return d


class TestStartNextDeployment:
    def test_starts_when_environment_idle(self, db_session, app_env, run_deploy_task):
        d = _trigger(db_session, app_env)

        assert d.started_at is not None
        run_deploy_task.assert_called_once_with(deployment_id=d.id)

    def test_queues_behind_running_deployment(
        self, db_session, app_env, run_deploy_task
    ):
        first = _trigger(db_session, app_env)
        second = _trigger(db_session, app_env)

        assert second.queued
        run_deploy_task.assert_called_once_with(deployment_id=first.id)

    def test_starts_oldest_queued_not_the_caller(
        self, db_session, app_env, run_deploy_task
    ):
        older = _deployment(db_session, app_env)
        newer = _deployment(db_session, app_env)

        start_next_deployment(app_env.id)

        assert older.started_at is not None
        assert newer.queued
        run_deploy_task.assert_called_once_with(deployment_id=older.id)

    def test_other_environments_are_not_blocked(
        self, db_session, project, application, app_env, run_deploy_task
    ):
        other_env = _app_env(db_session, project, application, "staging")
        _trigger(db_session, app_env)

        other = _trigger(db_session, other_env)

        assert other.started_at is not None

    @pytest.mark.parametrize("outcome", ["complete", "error"])
    def test_finished_deployment_starts_only_the_next(
        self, db_session, app_env, run_deploy_task, outcome
    ):
        running = _trigger(db_session, app_env)
        second = _trigger(db_session, app_env)
        third = _trigger(db_session, app_env)
        setattr(running, outcome, True)
        db_session.commit()

        start_next_deployment(app_env.id)
        start_next_deployment(app_env.id)

        assert second.started_at is not None
        assert third.queued


class TestRunDeployPromotes:
    @pytest.mark.parametrize("outcome", ["complete", "error"])
    def test_next_deploy_starts_when_task_finishes(
        self, db_session, app_env, run_deploy_task, outcome
    ):
        running = _trigger(db_session, app_env)
        queued = _trigger(db_session, app_env)
        run_deploy_task.reset_mock()

        def finish(deployment):
            setattr(deployment, outcome, True)
            db.session.commit()

        with patch("cabotage.celery.tasks.deploy.deploy_release", side_effect=finish):
            run_deploy(deployment_id=running.id)

        run_deploy_task.assert_called_once_with(deployment_id=queued.id)
        db_session.refresh(queued)
        assert queued.started_at is not None


class TestReaper:
    def _age(self, db_session, deployment):
        old = datetime.datetime.now(datetime.UTC).replace(
            tzinfo=None
        ) - datetime.timedelta(minutes=10)
        db_session.execute(
            sa.update(Deployment)
            .where(Deployment.id == deployment.id)
            .values(updated=old)
        )
        db_session.commit()

    def _reap(self, dead_ids):
        dead_keys = {heartbeat_key("deploy", str(i)) for i in dead_ids}
        redis_client = MagicMock()
        redis_client.exists.side_effect = lambda key: key not in dead_keys
        with (
            patch(
                "cabotage.celery.tasks.maintain.get_redis_client",
                return_value=redis_client,
            ),
            patch("cabotage.celery.tasks.maintain._dispatch_reap_failure"),
        ):
            reap_stale_builds()

    def test_queued_deployment_is_not_reaped(
        self, db_session, app_env, run_deploy_task
    ):
        _trigger(db_session, app_env)
        queued = _trigger(db_session, app_env)
        self._age(db_session, queued)

        self._reap([queued.id])

        db_session.refresh(queued)
        assert queued.queued

    def test_reaping_stuck_deployment_starts_next(
        self, db_session, app_env, run_deploy_task
    ):
        stuck = _trigger(db_session, app_env)
        queued = _trigger(db_session, app_env)
        self._age(db_session, stuck)
        run_deploy_task.reset_mock()

        self._reap([stuck.id])

        db_session.refresh(stuck)
        db_session.refresh(queued)
        assert stuck.error
        assert queued.started_at is not None
        run_deploy_task.assert_called_once_with(deployment_id=queued.id)
