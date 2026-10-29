"""Project overview page renders for both admins and view-only members."""

import time
import uuid
from unittest.mock import patch

import pytest
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import Application, Project
from cabotage.server.wsgi import app as _app


@pytest.fixture
def flask_app(monkeypatch):
    for key, value in {
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "REQUIRE_MFA": False,
    }.items():
        monkeypatch.setitem(_app.config, key, value)
    with _app.app_context(), patch.object(db.session, "commit", db.session.flush):
        try:
            yield _app
        finally:
            db.session.rollback()


def _client_for_project(flask_app, *, admin):
    user = User(
        username=f"member-{uuid.uuid4().hex[:8]}",
        email=f"member-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    org = Organization(name="Test Org", slug=f"testorg-{uuid.uuid4().hex[:8]}")
    db.session.add_all([user, org])
    db.session.flush()
    db.session.add(
        OrganizationMember(organization_id=org.id, user_id=user.id, admin=admin)
    )
    project = Project(name="My Project", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    db.session.add(Application(name="web", slug="web", project_id=project.id))
    db.session.flush()

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"
    return client, f"/projects/{org.slug}/{project.slug}"


@pytest.mark.parametrize("admin", [True, False])
def test_project_page_renders_for_org_members(flask_app, admin):
    client, url = _client_for_project(flask_app, admin=admin)

    resp = client.get(url)

    assert resp.status_code == 200
    assert (b'id="create-app-modal"' in resp.data) is admin
