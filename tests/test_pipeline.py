"""Regression tests for the pipeline stats/runs endpoints."""

import time
import uuid
from unittest.mock import patch

import pytest
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Project,
)
from cabotage.server.wsgi import app as _app


@pytest.fixture
def pipeline_app(monkeypatch):
    for key, value in {
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "REQUIRE_MFA": False,
    }.items():
        monkeypatch.setitem(_app.config, key, value)
    with _app.app_context(), patch.object(db.session, "commit", db.session.flush):
        try:
            yield _pipeline_app()
        finally:
            db.session.rollback()


def _pipeline_app():
    user = User(
        username=f"testadmin-{uuid.uuid4().hex[:8]}",
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    org = Organization(name="Test Org", slug=f"testorg-{uuid.uuid4().hex[:8]}")
    db.session.add_all([user, org])
    db.session.flush()
    db.session.add(
        OrganizationMember(organization_id=org.id, user_id=user.id, admin=True)
    )
    project = Project(name="My Project", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    environment = Environment(name="default", project_id=project.id, ephemeral=False)
    db.session.add(environment)
    db.session.flush()
    application = Application(name="web", slug="web", project_id=project.id)
    db.session.add(application)
    db.session.flush()
    app_env = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
        k8s_identifier=None,
    )
    db.session.add(app_env)
    db.session.flush()
    client = _app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"

    base = (
        f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/pipeline"
    )
    return client, application, app_env, base


def test_pipeline_window_rejects_overflowing_until(pipeline_app):
    client, _application, _app_env, base = pipeline_app
    response = client.get(f"{base}/stats?since=2024-01-01&until=9999-12-31")
    assert response.status_code == 400


def test_pipeline_window_accepts_valid_reversed_bounds(pipeline_app):
    client, _application, _app_env, base = pipeline_app
    # since/until reversed; should still be treated as an inclusive window.
    response = client.get(f"{base}/stats?since=2024-01-05&until=2024-01-01")
    assert response.status_code == 200
