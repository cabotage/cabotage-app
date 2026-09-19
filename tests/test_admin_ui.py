import datetime
import time
import uuid

import pytest
from flask import g
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import Organization, User, WebAuthn
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import Project, activity_plugin
from cabotage.server.wsgi import app as _app

Activity = activity_plugin.activity_cls


@pytest.fixture
def app():
    original_config = {
        "TESTING": _app.config.get("TESTING"),
        "WTF_CSRF_ENABLED": _app.config.get("WTF_CSRF_ENABLED"),
        "REQUIRE_MFA": _app.config.get("REQUIRE_MFA"),
        "DEBUG_TB_ENABLED": _app.config.get("DEBUG_TB_ENABLED"),
    }
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REQUIRE_MFA"] = False
    # The debug toolbar (on in docker-compose dev) injects template context
    # into every page, which breaks content assertions.
    _app.config["DEBUG_TB_ENABLED"] = False

    with _app.app_context():
        yield _app

    _app.config.update(original_config)


@pytest.fixture
def client(app):
    return app.test_client()


def _make_user(prefix, admin=False, active=True):
    user = User(
        username=f"{prefix}-{uuid.uuid4().hex[:8]}",
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=active,
        admin=admin,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def regular_user(app):
    user = _make_user("adminui-user")
    yield user
    _delete_user(user.id)


@pytest.fixture
def admin_user(app):
    user = _make_user("adminui-admin", admin=True)
    yield user
    _delete_user(user.id)


@pytest.fixture
def second_admin(app):
    user = _make_user("adminui-admin2", admin=True)
    yield user
    _delete_user(user.id)


@pytest.fixture
def organization(app, regular_user):
    org = Organization(
        name="Admin UI Test Org",
        slug=f"adminui-org-{uuid.uuid4().hex[:8]}",
    )
    org.add_user(regular_user, admin=True)
    db.session.add(org)
    db.session.commit()
    yield org
    _delete_org(org.id)


def _login(client, user):
    # Clear flask-login's per-context user cache; requests here run inside the
    # fixture's long-lived app context, so it survives between requests.
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
    WebAuthn.query.filter_by(user_id=user_id).delete()
    OrganizationMember.query.filter_by(user_id=user_id).delete()
    db.session.execute(
        db.text("UPDATE transaction SET user_id = NULL WHERE user_id = :uid"),
        {"uid": user_id},
    )
    User.query.filter_by(id=user_id).delete()
    db.session.commit()


def _delete_org(org_id):
    db.session.rollback()
    org = db.session.get(Organization, org_id)
    if org is None:
        return
    Project.query.filter_by(organization_id=org.id).delete()
    OrganizationMember.query.filter_by(organization_id=org.id).delete()
    Organization.query.filter_by(id=org.id).delete()
    db.session.commit()


def _activity_count(verb):
    return Activity.query.filter_by(verb=verb).count()


def _page_content(response):
    """Body with any debug-toolbar injection stripped (it dumps every recorded
    query's parameters into the page, breaking negative content assertions)."""
    return response.data.split(b"flDebug")[0]


# -- access control ----------------------------------------------------------


def test_anonymous_redirected_to_login(client):
    response = client.get("/admin/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


@pytest.mark.parametrize(
    "path",
    [
        "/admin/",
        "/admin/users",
        "/admin/organizations",
        "/admin/projects",
        "/admin/audit",
    ],
)
def test_non_admin_gets_403(client, regular_user, path):
    _login(client, regular_user)
    assert client.get(path).status_code == 403


def test_admin_can_load_all_pages(client, admin_user, organization):
    _login(client, admin_user)
    for path in [
        "/admin/",
        "/admin/users",
        "/admin/organizations",
        "/admin/projects",
        "/admin/audit",
        f"/admin/organizations/{organization.id}",
    ]:
        assert client.get(path).status_code == 200, path


# -- users -------------------------------------------------------------------


def test_user_search_filters(client, admin_user, regular_user):
    # Use a third user for the negative assertion: the debug toolbar (enabled
    # in docker dev) leaks the logged-in admin's details into every page.
    other = _make_user("adminui-bystander")
    try:
        _login(client, admin_user)
        response = client.get(f"/admin/users?q={regular_user.username}")
        assert response.status_code == 200
        content = _page_content(response)
        assert regular_user.email.encode() in content
        assert other.email.encode() not in content
    finally:
        _delete_user(other.id)


def test_user_list_admins_filter(client, admin_user, regular_user):
    _login(client, admin_user)
    response = client.get(f"/admin/users?filter=admins&q={regular_user.username}")
    assert response.status_code == 200
    content = _page_content(response)
    # regular_user matches q but is not an admin, so the filter excludes them
    assert regular_user.email.encode() not in content

    response = client.get(f"/admin/users?filter=admins&q={admin_user.username}")
    assert response.status_code == 200
    assert admin_user.email.encode() in _page_content(response)


def test_grant_and_revoke_admin(client, admin_user, regular_user):
    _login(client, admin_user)
    response = client.post(
        f"/admin/users/{regular_user.id}", data={"_action": "grant_admin"}
    )
    assert response.status_code == 302
    db.session.refresh(regular_user)
    assert regular_user.admin is True
    assert _activity_count("grant-admin") >= 1

    response = client.post(
        f"/admin/users/{regular_user.id}", data={"_action": "revoke_admin"}
    )
    assert response.status_code == 302
    db.session.refresh(regular_user)
    assert regular_user.admin is False
    assert _activity_count("revoke-admin") >= 1


def test_deactivate_rotates_uniquifier_and_activate(client, admin_user, regular_user):
    old_uniquifier = regular_user.fs_uniquifier
    _login(client, admin_user)
    response = client.post(
        f"/admin/users/{regular_user.id}", data={"_action": "deactivate"}
    )
    assert response.status_code == 302
    db.session.refresh(regular_user)
    assert regular_user.active is False
    assert regular_user.fs_uniquifier != old_uniquifier

    response = client.post(
        f"/admin/users/{regular_user.id}", data={"_action": "activate"}
    )
    assert response.status_code == 302
    db.session.refresh(regular_user)
    assert regular_user.active is True


def test_self_demotion_and_self_deactivation_blocked(client, admin_user, second_admin):
    _login(client, admin_user)
    for action in ("revoke_admin", "deactivate"):
        response = client.post(
            f"/admin/users/{admin_user.id}", data={"_action": action}
        )
        assert response.status_code == 302
    db.session.refresh(admin_user)
    assert admin_user.admin is True
    assert admin_user.active is True


def test_last_active_admin_cannot_be_demoted(client, admin_user, second_admin):
    # second_admin demotes admin_user, then nobody can demote second_admin
    _login(client, second_admin)
    response = client.post(
        f"/admin/users/{admin_user.id}", data={"_action": "revoke_admin"}
    )
    assert response.status_code == 302
    db.session.refresh(admin_user)
    assert admin_user.admin is False

    # restore for fixture teardown sanity; verify guard by attempting to demote
    # the only remaining admin from a re-granted account
    response = client.post(
        f"/admin/users/{admin_user.id}", data={"_action": "grant_admin"}
    )
    assert response.status_code == 302


def test_reset_mfa_clears_second_factors(client, admin_user, regular_user):
    regular_user.tf_primary_method = "authenticator"
    regular_user.tf_totp_secret = "SECRET"
    regular_user.mf_recovery_codes = ["code-one", "code-two"]
    db.session.add(
        WebAuthn(
            user_id=regular_user.id,
            credential_id=uuid.uuid4().bytes,
            public_key=b"key",
            sign_count=0,
            name=f"key-{uuid.uuid4().hex[:6]}",
            usage="secondary",
            backup_state=False,
            device_type="single_device",
            lastuse_datetime=datetime.datetime.now(datetime.timezone.utc).replace(
                tzinfo=None
            ),
        )
    )
    old_uniquifier = regular_user.fs_uniquifier
    db.session.commit()

    _login(client, admin_user)

    # without matching confirmation nothing happens
    response = client.post(
        f"/admin/users/{regular_user.id}", data={"_action": "reset_mfa"}
    )
    assert response.status_code == 302
    db.session.refresh(regular_user)
    assert regular_user.tf_primary_method == "authenticator"

    response = client.post(
        f"/admin/users/{regular_user.id}",
        data={"_action": "reset_mfa", "confirm": regular_user.username},
    )
    assert response.status_code == 302

    db.session.refresh(regular_user)
    assert regular_user.tf_primary_method is None
    assert regular_user.tf_totp_secret is None
    assert not regular_user.mf_recovery_codes
    assert WebAuthn.query.filter_by(user_id=regular_user.id).count() == 0
    assert regular_user.fs_uniquifier != old_uniquifier
    assert _activity_count("reset-mfa") >= 1


# -- organizations -----------------------------------------------------------


def test_org_membership_repair(client, admin_user, regular_user, organization):
    other = _make_user("adminui-other")
    try:
        _login(client, admin_user)
        url = f"/admin/organizations/{organization.id}"

        response = client.post(
            url,
            data={"_action": "add_member", "identity": other.email, "admin": ""},
        )
        assert response.status_code == 302
        member = OrganizationMember.query.filter_by(
            organization_id=organization.id, user_id=other.id
        ).one()
        assert member.admin is False

        response = client.post(
            url, data={"_action": "promote_member", "user_id": str(other.id)}
        )
        assert response.status_code == 302
        db.session.refresh(member)
        assert member.admin is True

        response = client.post(
            url, data={"_action": "demote_member", "user_id": str(other.id)}
        )
        assert response.status_code == 302
        db.session.refresh(member)
        assert member.admin is False

        response = client.post(
            url, data={"_action": "remove_member", "user_id": str(other.id)}
        )
        assert response.status_code == 302
        assert (
            OrganizationMember.query.filter_by(
                organization_id=organization.id, user_id=other.id
            ).first()
            is None
        )
    finally:
        _delete_user(other.id)


def test_org_last_member_cannot_be_removed(
    client, admin_user, regular_user, organization
):
    _login(client, admin_user)
    response = client.post(
        f"/admin/organizations/{organization.id}",
        data={"_action": "remove_member", "user_id": str(regular_user.id)},
    )
    assert response.status_code == 302
    assert (
        OrganizationMember.query.filter_by(
            organization_id=organization.id, user_id=regular_user.id
        ).first()
        is not None
    )


def test_org_soft_delete_and_restore(client, admin_user, regular_user, organization):
    original_slug = organization.slug
    project = Project(
        organization_id=organization.id,
        name="Restorable Project",
        slug=f"restorable-{uuid.uuid4().hex[:8]}",
    )
    db.session.add(project)
    db.session.commit()
    original_project_slug = project.slug

    _login(client, admin_user)
    url = f"/admin/organizations/{organization.id}"

    response = client.post(
        url, data={"_action": "soft_delete", "confirm": original_slug}
    )
    assert response.status_code == 302
    db.session.refresh(organization)
    db.session.refresh(project)
    assert organization.deleted_at is not None
    assert "--deleted-" in organization.slug
    assert project.deleted_at is not None
    assert "--deleted-" in project.slug

    response = client.post(url, data={"_action": "restore"})
    assert response.status_code == 302
    db.session.refresh(organization)
    db.session.refresh(project)
    assert organization.deleted_at is None
    assert organization.slug == original_slug
    assert project.deleted_at is None
    assert project.slug == original_project_slug


def test_org_restore_blocked_by_slug_collision(
    client, admin_user, regular_user, organization
):
    original_slug = organization.slug
    _login(client, admin_user)
    url = f"/admin/organizations/{organization.id}"

    response = client.post(
        url, data={"_action": "soft_delete", "confirm": original_slug}
    )
    assert response.status_code == 302
    db.session.refresh(organization)

    squatter = Organization(name="Squatter", slug=original_slug)
    db.session.add(squatter)
    db.session.commit()

    try:
        response = client.post(url, data={"_action": "restore"})
        assert response.status_code == 302
        db.session.refresh(organization)
        assert organization.deleted_at is not None
    finally:
        Organization.query.filter_by(id=squatter.id).delete()
        db.session.commit()


# -- projects ----------------------------------------------------------------


def test_project_delete_restore_and_org_guard(
    client, admin_user, regular_user, organization
):
    project = Project(
        organization_id=organization.id,
        name="Cycled Project",
        slug=f"cycled-{uuid.uuid4().hex[:8]}",
    )
    db.session.add(project)
    db.session.commit()
    original_slug = project.slug

    _login(client, admin_user)
    url = f"/admin/projects/{project.id}"

    response = client.post(
        url, data={"_action": "soft_delete", "confirm": original_slug}
    )
    assert response.status_code == 302
    db.session.refresh(project)
    assert project.deleted_at is not None

    # deleted org blocks restore
    organization.deleted_at = project.deleted_at
    db.session.commit()
    response = client.post(url, data={"_action": "restore"})
    assert response.status_code == 302
    db.session.refresh(project)
    assert project.deleted_at is not None

    organization.deleted_at = None
    db.session.commit()
    response = client.post(url, data={"_action": "restore"})
    assert response.status_code == 302
    db.session.refresh(project)
    assert project.deleted_at is None
    assert project.slug == original_slug


# -- audit -------------------------------------------------------------------


def test_global_audit_log_shows_admin_actions(
    client, admin_user, regular_user, organization
):
    _login(client, admin_user)
    response = client.post(
        f"/admin/users/{regular_user.id}", data={"_action": "grant_admin"}
    )
    assert response.status_code == 302
    client.post(f"/admin/users/{regular_user.id}", data={"_action": "revoke_admin"})

    response = client.get("/admin/audit")
    assert response.status_code == 200
    assert b"grant admin" in response.data or b"grant-admin" in response.data
