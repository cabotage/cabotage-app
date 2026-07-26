"""Shared fixtures. Modules may still override any of these locally."""

import uuid
from unittest.mock import patch

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
    e = Environment(name="default", project_id=project.id, ephemeral=False)
    db_session.add(e)
    db_session.flush()
    return e


@pytest.fixture
def application(db_session, project):
    a = Application(name="webapp", slug="webapp", project_id=project.id)
    db_session.add(a)
    db_session.flush()
    return a


@pytest.fixture
def make_environment(db_session, project):
    def _make(slug, name=None, **kwargs):
        e = Environment(
            name=name or slug,
            slug=slug,
            project_id=project.id,
            ephemeral=False,
            **kwargs,
        )
        db_session.add(e)
        db_session.flush()
        return e

    return _make


@pytest.fixture
def make_app_env(db_session):
    def _make(application, environment, **kwargs):
        app_env = ApplicationEnvironment(
            application_id=application.id,
            environment_id=environment.id,
            **kwargs,
        )
        db_session.add(app_env)
        db_session.flush()
        return app_env

    return _make


@pytest.fixture
def no_k8s_cleanup():
    """Deletion paths queue k8s cleanup; the semantics under test are DB-side."""
    with patch("cabotage.server.user.views._enqueue_app_env_cleanup") as enqueue:
        yield enqueue
