"""Tests for the authenticated dashboard at ``/``."""

import datetime
import re
import time
import uuid

import pytest
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Deployment,
    Environment,
    Project,
)
from cabotage.server.wsgi import app as _app


@pytest.fixture
def app():
    original = {
        key: _app.config.get(key)
        for key in ("TESTING", "WTF_CSRF_ENABLED", "REQUIRE_MFA")
    }
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REQUIRE_MFA"] = False
    with _app.app_context():
        yield _app
    _app.config.update(original)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db_session(app):
    yield db.session
    db.session.rollback()


def _user(db_session, *, admin=False):
    user = User(
        username=f"dash-{uuid.uuid4().hex[:8]}",
        email=f"dash-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        admin=admin,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _org(db_session, member=None):
    slug = f"dash-org-{uuid.uuid4().hex[:8]}"
    org = Organization(name=slug, slug=slug)
    db_session.add(org)
    db_session.flush()
    if member is not None:
        db_session.add(
            OrganizationMember(organization_id=org.id, user_id=member.id, admin=False)
        )
        db_session.flush()
    return org


def _app_env(db_session, org, slug):
    project = Project(
        name=f"proj-{slug}",
        slug=f"proj-{slug}",
        organization_id=org.id,
        environments_enabled=False,
    )
    db_session.add(project)
    db_session.flush()
    env = Environment(name="Default", slug="default", project_id=project.id)
    db_session.add(env)
    db_session.flush()
    application = Application(name=slug, slug=slug, project_id=project.id)
    db_session.add(application)
    db_session.flush()
    ae = ApplicationEnvironment(
        application_id=application.id, environment_id=env.id, k8s_identifier=None
    )
    db_session.add(ae)
    db_session.flush()
    return application, ae


def _deploy(db_session, application, ae, *, minutes_ago, complete, error):
    db_session.add(
        Deployment(
            application_id=application.id,
            application_environment_id=ae.id,
            release={},
            complete=complete,
            error=error,
            created=datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
            - datetime.timedelta(minutes=minutes_ago),
        )
    )
    db_session.flush()


def _login(client, user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"


def _app_status(html, slug):
    match = re.search(
        rf'data-status="([a-z-]+)"\s+data-search="[^"]* {re.escape(slug)}"', html
    )
    assert match, f"app row {slug} not rendered"
    return match.group(1)


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        # An old failure followed by a good deploy is healthy.
        ([(30, True, False), (10, False, True)], "live"),
        # Only the most recent deployment decides a failure.
        ([(30, False, True), (10, True, False)], "deploy-failed"),
        ([(30, False, True), (1, False, False)], "deploying"),
        ([], "idle"),
    ],
)
def test_app_status_reflects_latest_deployment(client, db_session, history, expected):
    user = _user(db_session)
    org = _org(db_session, member=user)
    slug = f"app-{uuid.uuid4().hex[:8]}"
    application, ae = _app_env(db_session, org, slug)
    for minutes_ago, error, complete in history:
        _deploy(
            db_session,
            application,
            ae,
            minutes_ago=minutes_ago,
            complete=complete,
            error=error,
        )
    _login(client, user)

    response = client.get("/")

    assert response.status_code == 200
    assert _app_status(response.get_data(as_text=True), slug) == expected


def test_all_orgs_scope_is_ignored_for_non_admins(client, db_session):
    member = _user(db_session)
    mine = _org(db_session, member=member)
    other = _org(db_session)
    _login(client, member)

    html = client.get("/?scope=all").get_data(as_text=True)

    assert mine.slug in html
    assert other.slug not in html
    assert "Admin view." not in html


def test_all_orgs_scope_shows_non_member_orgs_to_admins(client, db_session):
    admin = _user(db_session, admin=True)
    other = _org(db_session)
    _org(db_session, member=admin)
    _login(client, admin)

    assert other.slug not in client.get("/").get_data(as_text=True)

    html = client.get("/?scope=all").get_data(as_text=True)
    assert "Admin view." in html
    assert other.slug in html
    assert "Not a member" in html
