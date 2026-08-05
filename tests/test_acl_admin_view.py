import time
import uuid

import pytest
from flask import g
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import Application, Environment, Project
from cabotage.server.wsgi import app as _app


@pytest.fixture
def app():
    original_config = {
        "TESTING": _app.config.get("TESTING"),
        "WTF_CSRF_ENABLED": _app.config.get("WTF_CSRF_ENABLED"),
        "REQUIRE_MFA": _app.config.get("REQUIRE_MFA"),
        "DEBUG_TB_ENABLED": _app.config.get("DEBUG_TB_ENABLED"),
        "EXT_PREFERRED_URL_SCHEME": _app.config.get("EXT_PREFERRED_URL_SCHEME"),
        "EXT_SERVER_NAME": _app.config.get("EXT_SERVER_NAME"),
    }
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REQUIRE_MFA"] = False
    # The debug toolbar (on in docker-compose dev) injects template context
    # into every page, which breaks content assertions.
    _app.config["DEBUG_TB_ENABLED"] = False
    # Org settings renders oidc.issuer_url(), which requires HTTPS.
    _app.config["EXT_PREFERRED_URL_SCHEME"] = "https"
    _app.config["EXT_SERVER_NAME"] = "localhost"

    with _app.app_context():
        yield _app

    _app.config.update(original_config)


@pytest.fixture
def client(app):
    return app.test_client()


def _make_user(prefix, admin=False):
    user = User(
        username=f"{prefix}-{uuid.uuid4().hex[:8]}",
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        admin=admin,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def regular_user(app):
    user = _make_user("acl-user")
    yield user
    _delete_user(user.id)


@pytest.fixture
def admin_user(app):
    user = _make_user("acl-admin", admin=True)
    yield user
    _delete_user(user.id)


@pytest.fixture
def member_user(app):
    user = _make_user("acl-member")
    yield user
    _delete_user(user.id)


@pytest.fixture
def organization(app, member_user):
    org = Organization(
        name="ACL Test Org",
        slug=f"acl-test-org-{uuid.uuid4().hex[:8]}",
    )
    org.add_user(member_user, admin=True)
    db.session.add(org)
    db.session.commit()
    yield org
    _delete_org(org.id)


@pytest.fixture
def project(app, organization):
    proj = Project(
        organization_id=organization.id,
        name="ACL Test Project",
        slug=f"acl-test-project-{uuid.uuid4().hex[:8]}",
    )
    db.session.add(proj)
    db.session.commit()
    yield proj
    db.session.rollback()
    Project.query.filter_by(id=proj.id).delete()
    db.session.commit()


def _login(client, user):
    # Requests in these tests run inside the fixture's long-lived app context,
    # so flask-login's per-context user cache survives between requests — clear
    # it or a user switch within one test keeps resolving the previous user.
    for attr in ("_login_user", "fs_authn_via", "fs_paa"):
        if attr in g:
            g.pop(attr)
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"


def _delete_user(user_id):
    db.session.rollback()
    OrganizationMember.query.filter_by(user_id=user_id).delete()
    db.session.execute(
        db.text("UPDATE transaction SET user_id = NULL WHERE user_id = :uid"),
        {"uid": user_id},
    )
    User.query.filter_by(id=user_id).delete()
    db.session.commit()


def _delete_org(org_id):
    db.session.rollback()
    org = Organization.query.filter_by(id=org_id).first()
    if not org:
        return
    project_ids = [p.id for p in Project.query.filter_by(organization_id=org.id).all()]
    if project_ids:
        Environment.query.filter(Environment.project_id.in_(project_ids)).delete(
            synchronize_session=False
        )
        Application.query.filter(Application.project_id.in_(project_ids)).delete(
            synchronize_session=False
        )
        Project.query.filter(Project.id.in_(project_ids)).delete(
            synchronize_session=False
        )
    OrganizationMember.query.filter_by(organization_id=org.id).delete()
    Organization.query.filter_by(id=org.id).delete()
    db.session.commit()


def test_regular_non_member_cannot_view_organization(
    client, regular_user, organization
):
    _login(client, regular_user)
    response = client.get(f"/organizations/{organization.slug}")
    assert response.status_code == 403


def test_admin_non_member_can_view_organization(client, admin_user, organization):
    _login(client, admin_user)
    response = client.get(f"/organizations/{organization.slug}")
    assert response.status_code == 200


def test_admin_non_member_can_view_project(client, admin_user, organization, project):
    _login(client, admin_user)
    response = client.get(f"/projects/{organization.slug}/{project.slug}")
    assert response.status_code == 200


def test_regular_non_member_cannot_view_project(
    client, regular_user, organization, project
):
    _login(client, regular_user)
    response = client.get(f"/projects/{organization.slug}/{project.slug}")
    assert response.status_code == 403


def test_admin_non_member_can_view_settings_readonly(
    client, admin_user, regular_user, organization, project
):
    _login(client, admin_user)
    for path in (
        f"/organizations/{organization.slug}/settings",
        f"/projects/{organization.slug}/{project.slug}/settings",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert b"super-admin access" in response.data

    _login(client, regular_user)
    for path in (
        f"/organizations/{organization.slug}/settings",
        f"/projects/{organization.slug}/{project.slug}/settings",
    ):
        assert client.get(path).status_code == 403, path

    # An ordinary (non-org-admin) member must not see settings either —
    # only org admins and super admins may.
    plain_member = _make_user("acl-plain-member")
    try:
        organization.add_user(plain_member, admin=False)
        db.session.commit()
        _login(client, plain_member)
        for path in (
            f"/organizations/{organization.slug}/settings",
            f"/projects/{organization.slug}/{project.slug}/settings",
        ):
            assert client.get(path).status_code == 403, path
    finally:
        _delete_user(plain_member.id)


def test_admin_non_member_cannot_post_org_settings(client, admin_user, organization):
    _login(client, admin_user)
    response = client.post(
        f"/organizations/{organization.slug}/settings",
        data={"_action": "save_org", "name": "Hijacked"},
    )
    assert response.status_code == 403


def test_admin_non_member_cannot_create_project(client, admin_user, organization):
    _login(client, admin_user)
    response = client.post(
        f"/organizations/{organization.slug}/projects/create",
        data={
            "organization_id": str(organization.id),
            "name": "Sneaky Project",
            "slug": "sneaky-project",
        },
    )
    assert response.status_code == 403


def test_member_can_create_project(client, member_user, organization):
    _login(client, member_user)
    slug = f"member-project-{uuid.uuid4().hex[:8]}"
    response = client.post(
        f"/organizations/{organization.slug}/projects/create",
        data={
            "organization_id": str(organization.id),
            "name": "Member Project",
            "slug": slug,
        },
    )
    assert response.status_code == 302
    assert Project.query.filter_by(slug=slug).first() is not None


def test_admin_banner_shown_for_non_member(client, admin_user, organization):
    _login(client, admin_user)
    response = client.get(f"/organizations/{organization.slug}")
    assert response.status_code == 200
    assert b"super-admin access" in response.data


def test_admin_banner_absent_for_member(client, member_user, organization):
    _login(client, member_user)
    response = client.get(f"/organizations/{organization.slug}")
    assert response.status_code == 200
    assert b"super-admin access" not in response.data


def test_admin_non_member_does_not_see_org_admin_controls(
    client, admin_user, organization
):
    _login(client, admin_user)
    response = client.get(f"/organizations/{organization.slug}/members")
    assert response.status_code == 200
    assert b"Add Member" not in response.data


def test_flask_admin_serves_from_admin_db(client, regular_user, admin_user):
    _login(client, admin_user)
    response = client.get("/admin/db/")
    assert response.status_code == 200
    # It must render flask-admin itself, not the admin_ui dashboard — the
    # admin_ui templates once shadowed flask-admin's "admin/index.html".
    assert b"/admin/db/_user/" in response.data

    _login(client, regular_user)
    response = client.get("/admin/db/")
    assert response.status_code == 403


def test_flask_admin_user_details_redact_secrets(client, admin_user, member_user):
    # Set the secret on a user other than the logged-in admin: the debug
    # toolbar (enabled in docker dev) leaks the logged-in user's details.
    marker = f"SECRETMARKER{uuid.uuid4().hex}"
    member_user.tf_totp_secret = marker
    db.session.commit()

    _login(client, admin_user)
    response = client.get(f"/admin/db/_user/details/?id={member_user.id}")
    assert response.status_code == 200
    # Strip debug-toolbar injection: it dumps recorded query parameters
    # (including the UPDATE that set the marker) into the page.
    content = response.data.split(b"flDebug")[0]
    assert member_user.email.encode() in content
    assert marker.encode() not in content

    member_user.tf_totp_secret = None
    db.session.commit()
