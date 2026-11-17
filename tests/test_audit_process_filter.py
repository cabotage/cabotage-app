"""The audit log can be filtered down to a single Procfile process."""

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
    activity_plugin,
)
from cabotage.server.wsgi import app as _app

Activity = activity_plugin.activity_cls


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


def _scale(application, process, old, new):
    db.session.add(
        Activity(
            verb="scale",
            object=application,
            data={
                "user_id": "tester",
                "timestamp": "2026-01-01T00:00:00",
                "changes": {
                    process: {"process_count": {"old_value": old, "new_value": new}}
                },
            },
        )
    )
    db.session.flush()


@pytest.fixture
def audit_client(flask_app):
    user = User(
        username=f"member-{uuid.uuid4().hex[:8]}",
        email=f"member-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    org = Organization(name="Audit Org", slug=f"auditorg-{uuid.uuid4().hex[:8]}")
    db.session.add_all([user, org])
    db.session.flush()
    db.session.add(
        OrganizationMember(organization_id=org.id, user_id=user.id, admin=True)
    )
    project = Project(name="Audit Project", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    env = Environment(name="default", project_id=project.id, ephemeral=False)
    db.session.add(env)
    db.session.flush()
    application = Application(name="webapp", slug="webapp", project_id=project.id)
    db.session.add(application)
    db.session.flush()
    db.session.add(
        ApplicationEnvironment(application_id=application.id, environment_id=env.id)
    )
    db.session.flush()

    _scale(application, "web", 1, 2)
    _scale(application, "worker", 0, 3)

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"
    return (
        client,
        f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/audit",
    )


def _rows(html):
    return html.count('class="audit-row"')


def test_without_process_filter_shows_every_scale_event(audit_client):
    client, url = audit_client
    assert _rows(client.get(url).get_data(as_text=True)) == 2


def test_process_filter_keeps_only_that_process(audit_client):
    client, url = audit_client
    html = client.get(url + "?process=worker").get_data(as_text=True)
    assert _rows(html) == 1
    assert "process: worker" in html


def test_process_filter_shows_a_removable_pill(audit_client):
    client, url = audit_client
    html = client.get(url + "?process=web").get_data(as_text=True)
    assert _rows(html) == 1
    assert 'data-filter-type="process"' in html


def test_unknown_process_shows_no_entries(audit_client):
    client, url = audit_client
    assert _rows(client.get(url + "?process=nope").get_data(as_text=True)) == 0
