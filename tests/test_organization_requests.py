import time
import uuid

import pytest
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import Organization, OrganizationRequest, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.wsgi import app as _app


@pytest.fixture
def app():
    original_config = {
        "TESTING": _app.config.get("TESTING"),
        "WTF_CSRF_ENABLED": _app.config.get("WTF_CSRF_ENABLED"),
        "REQUIRE_MFA": _app.config.get("REQUIRE_MFA"),
        "ORGANIZATION_REQUESTS_ENABLED": _app.config.get(
            "ORGANIZATION_REQUESTS_ENABLED"
        ),
    }
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REQUIRE_MFA"] = False

    with _app.app_context():
        yield _app

    _app.config.update(original_config)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def regular_user(app):
    user = User(
        username=f"org-request-user-{uuid.uuid4().hex[:8]}",
        email=f"org-request-user-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db.session.add(user)
    db.session.commit()
    yield user
    _delete_user(user.id)


@pytest.fixture
def admin_user(app):
    user = User(
        username=f"org-request-admin-{uuid.uuid4().hex[:8]}",
        email=f"org-request-admin-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        admin=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db.session.add(user)
    db.session.commit()
    yield user
    _delete_user(user.id)


def _login(client, user):
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
    OrganizationRequest.query.filter(
        (OrganizationRequest.requester_user_id == user_id)
        | (OrganizationRequest.reviewer_user_id == user_id)
    ).delete(synchronize_session=False)
    db.session.execute(
        db.text("UPDATE transaction SET user_id = NULL WHERE user_id = :uid"),
        {"uid": user_id},
    )
    User.query.filter_by(id=user_id).delete()
    db.session.commit()


def _delete_org(slug):
    org = Organization.query.filter_by(slug=slug).first()
    if not org:
        return
    OrganizationRequest.query.filter_by(organization_id=org.id).update(
        {"organization_id": None}
    )
    OrganizationMember.query.filter_by(organization_id=org.id).delete()
    Organization.query.filter_by(id=org.id).delete()
    db.session.commit()


def test_request_routes_are_hidden_when_disabled(client, regular_user):
    client.application.config["ORGANIZATION_REQUESTS_ENABLED"] = False
    _login(client, regular_user)

    response = client.get("/")
    assert response.status_code == 200
    assert b"administrator will need to add you" in response.data
    assert b"Request Organization" not in response.data

    response = client.get("/organization-requests/create")
    assert response.status_code == 404


def test_user_can_submit_organization_request_when_enabled(client, regular_user):
    client.application.config["ORGANIZATION_REQUESTS_ENABLED"] = True
    _login(client, regular_user)
    slug = f"requested-org-{uuid.uuid4().hex[:8]}"

    response = client.get("/")
    assert response.status_code == 200
    assert b"Request Organization" in response.data

    response = client.post(
        "/organization-requests/create",
        data={
            "name": "Requested Org",
            "slug": slug,
            "note": "Please create this for my team.",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    org_request = OrganizationRequest.query.filter_by(slug=slug).one()
    assert org_request.requester_user_id == regular_user.id
    assert org_request.status == OrganizationRequest.STATUS_PENDING
    assert org_request.note == "Please create this for my team."


def test_non_admin_cannot_view_organization_requests(client, regular_user):
    client.application.config["ORGANIZATION_REQUESTS_ENABLED"] = True
    _login(client, regular_user)
    response = client.get("/organization-requests")
    assert response.status_code == 403


def test_admin_can_view_organization_requests(client, regular_user, admin_user):
    client.application.config["ORGANIZATION_REQUESTS_ENABLED"] = True
    slug = f"viewable-org-{uuid.uuid4().hex[:8]}"
    org_request = OrganizationRequest(
        requester_user_id=regular_user.id,
        name="Viewable Org",
        slug=slug,
        status=OrganizationRequest.STATUS_PENDING,
    )
    db.session.add(org_request)
    db.session.commit()

    _login(client, admin_user)
    response = client.get("/organization-requests")
    assert response.status_code == 200
    assert b"Viewable Org" in response.data


def test_admin_can_approve_organization_request(client, regular_user, admin_user):
    client.application.config["ORGANIZATION_REQUESTS_ENABLED"] = True
    slug = f"approved-org-{uuid.uuid4().hex[:8]}"
    org_request = OrganizationRequest(
        requester_user_id=regular_user.id,
        name="Approved Org",
        slug=slug,
        status=OrganizationRequest.STATUS_PENDING,
    )
    db.session.add(org_request)
    db.session.commit()
    request_id = org_request.id

    _login(client, admin_user)
    response = client.post(
        f"/organization-requests/{request_id}/approve",
        follow_redirects=False,
    )
    assert response.status_code == 302

    db.session.refresh(org_request)
    organization = Organization.query.filter_by(slug=slug).one()
    membership = OrganizationMember.query.filter_by(
        organization_id=organization.id,
        user_id=regular_user.id,
    ).one()
    assert membership.admin is True
    assert org_request.status == OrganizationRequest.STATUS_APPROVED
    assert org_request.reviewer_user_id == admin_user.id
    assert org_request.organization_id == organization.id
    _delete_org(slug)


def test_admin_can_deny_organization_request(client, regular_user, admin_user):
    client.application.config["ORGANIZATION_REQUESTS_ENABLED"] = True
    slug = f"denied-org-{uuid.uuid4().hex[:8]}"
    org_request = OrganizationRequest(
        requester_user_id=regular_user.id,
        name="Denied Org",
        slug=slug,
        status=OrganizationRequest.STATUS_PENDING,
    )
    db.session.add(org_request)
    db.session.commit()
    request_id = org_request.id

    _login(client, admin_user)
    response = client.post(
        f"/organization-requests/{request_id}/deny",
        follow_redirects=False,
    )
    assert response.status_code == 302

    db.session.refresh(org_request)
    assert org_request.status == OrganizationRequest.STATUS_DENIED
    assert org_request.reviewer_user_id == admin_user.id
    assert Organization.query.filter_by(slug=slug).first() is None
