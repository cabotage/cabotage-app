"""A redelivered deploy task must not roll back a deployment that already ran,
but queued deployments must still run in order."""

import datetime
import uuid
from unittest.mock import patch

import pytest

from cabotage.celery.tasks.deploy import _newer_deployment
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

T0 = datetime.datetime(2026, 10, 27, 5, 0, tzinfo=datetime.UTC)


@pytest.fixture
def app_envs():
    with _app.app_context(), patch.object(db.session, "commit", db.session.flush):
        try:
            org = Organization(name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
            db.session.add(org)
            db.session.flush()
            project = Project(name="Project", organization_id=org.id)
            db.session.add(project)
            db.session.flush()
            application = Application(name="web", slug="web", project_id=project.id)
            db.session.add(application)
            db.session.flush()
            envs = []
            for name in ("default", "staging"):
                environment = Environment(
                    name=name, project_id=project.id, ephemeral=False
                )
                db.session.add(environment)
                db.session.flush()
                app_env = ApplicationEnvironment(
                    application_id=application.id, environment_id=environment.id
                )
                db.session.add(app_env)
                db.session.flush()
                envs.append(app_env)
            yield envs
        finally:
            db.session.rollback()


def _deployment(app_env, minutes, *, job_id=None, complete=False, error=False):
    deployment = Deployment(
        application_id=app_env.application_id,
        application_environment_id=app_env.id,
        release={},
        job_id=job_id,
        complete=complete,
        error=error,
        created=T0 + datetime.timedelta(minutes=minutes),
    )
    db.session.add(deployment)
    db.session.flush()
    return deployment


@pytest.mark.parametrize(
    "state",
    [
        {"job_id": "abcd1234"},
        {"job_id": "abcd1234", "complete": True},
        {"job_id": "abcd1234", "error": True},
    ],
    ids=["running", "complete", "failed"],
)
def test_started_later_deployment_supersedes(app_envs, state):
    app_env, _ = app_envs
    stale = _deployment(app_env, 0, job_id="0000aaaa")
    newer = _deployment(app_env, 1, **state)

    assert _newer_deployment(stale) == newer


def test_queued_later_deployment_does_not_supersede(app_envs):
    app_env, _ = app_envs
    current = _deployment(app_env, 0, job_id="0000aaaa")
    _deployment(app_env, 1)

    assert _newer_deployment(current) is None


def test_earlier_deployment_does_not_supersede(app_envs):
    app_env, _ = app_envs
    _deployment(app_env, 0, job_id="0000aaaa", complete=True)
    current = _deployment(app_env, 1, job_id="1111bbbb")

    assert _newer_deployment(current) is None


def test_other_environment_does_not_supersede(app_envs):
    app_env, other_env = app_envs
    current = _deployment(app_env, 0, job_id="0000aaaa")
    _deployment(other_env, 1, job_id="1111bbbb", complete=True)

    assert _newer_deployment(current) is None
