"""Behavioral boundaries use real ES256 signatures, never a stubbed verifier."""

import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from flask import g
from sqlalchemy import delete

from cabotage.server import db, security
from flask import abort, redirect, request
from flask_login import login_required
from cabotage.server.admin_passkey import require_admin_action, require_admin_session
from cabotage.server.models.admin_security import AdminChallenge, AdminGrant
from cabotage.server.models.auth import (
    DiscordIntegration,
    Organization,
    SlackIntegration,
    WebAuthn,
)
from tests.admin_passkey_helpers import SigningPasskey
from tests.test_organization_requests import (
    _delete_org,
    _login,
    admin_user as admin_user,
    app as app,
    client as client,
    regular_user as regular_user,
)


@pytest.fixture(autouse=True)
def _admin_security_config(app, monkeypatch):
    monkeypatch.setitem(app.config, "EXT_PREFERRED_URL_SCHEME", "https")

    @login_required
    def protected_action(org_slug, operation):
        verification = require_admin_session()
        if verification is not None:
            return verification
        organization = Organization.query.filter_by(slug=org_slug).first_or_404()
        if request.method == "GET":
            return organization.name
        verification = require_admin_action("test.protected_action", request.path, {})
        if verification is not None:
            return verification
        if operation != "settings":
            abort(400)
        organization.name = request.form["name"]
        db.session.commit()
        return redirect(request.path)

    with monkeypatch.context() as setup:
        setup.setattr(app, "_got_first_request", False)
        if "test.protected_action" not in app.view_functions:
            app.add_url_rule(
                "/test/admin-action/<org_slug>/<operation>",
                endpoint="test.protected_action",
                view_func=protected_action,
                methods=["GET", "POST"],
            )


@pytest.fixture
def passkey(admin_user):
    key = SigningPasskey.register(admin_user)
    yield key
    db.session.rollback()
    db.session.execute(
        delete(AdminChallenge).where(AdminChallenge.user_id == admin_user.id)
    )
    db.session.execute(delete(AdminGrant).where(AdminGrant.user_id == admin_user.id))
    WebAuthn.query.filter_by(user_id=admin_user.id).delete()
    db.session.commit()


@pytest.fixture
def tenant(app):
    organization = Organization(
        name="Other tenant", slug=f"passkey-{uuid.uuid4().hex[:10]}"
    )
    db.session.add(organization)
    db.session.commit()
    yield organization
    db.session.rollback()
    DiscordIntegration.query.filter_by(organization_id=organization.id).delete()
    SlackIntegration.query.filter_by(organization_id=organization.id).delete()
    _delete_org(organization.slug)


def _settings(tenant):
    return f"/test/admin-action/{tenant.slug}/settings"


def _data(tenant, name="Changed tenant"):
    return {"_action": "save_org", "organization_id": str(tenant.id), "name": name}


def _proof(client, passkey, tenant):
    response = client.post(
        _settings(tenant), data=_data(tenant), headers={"X-Admin-Fetch": "1"}
    )
    assert response.status_code == 428
    result = passkey.verify(
        client, response.get_json()["admin_verification"]["request_id"]
    )
    assert result.status_code == 200
    return result.get_json()["action_token"]


def test_admin_flag_and_totp_are_not_entry_authority(client, admin_user, tenant):
    admin_user.tf_primary_method = "authenticator"
    db.session.commit()
    _login(client, admin_user)
    assert client.get(f"/organizations/{tenant.slug}/settings").status_code == 403
    assert client.get("/admin/").status_code == 302
    assert client.post("/admin/passkey/options", json={}).status_code == 403
    assert AdminChallenge.query.filter_by(user_id=admin_user.id).count() == 0
    for _ in range(3):
        response = client.post("/admin/passkey/options", json={})
        assert response.status_code == 403
        assert "Register a user-verifying passkey" in response.get_json()["error"]
    assert AdminChallenge.query.filter_by(user_id=admin_user.id).count() == 0


def test_nonadmin_cannot_mint_entry_challenge(client, regular_user):
    _login(client, regular_user)
    assert client.post("/admin/passkey/options", json={}).status_code == 403


@pytest.mark.parametrize(
    "invalid",
    [
        {"uv": False},
        {"origin": "https://attacker.invalid"},
        {"rp_id": "attacker.invalid"},
        {"challenge": "d3Jvbmc"},
        {"user_handle": "another-user"},
    ],
)
def test_assertion_binding_and_uv_are_verified(client, admin_user, passkey, invalid):
    _login(client, admin_user)
    assert passkey.verify(client, **invalid).status_code == 403
    with client.session_transaction() as state:
        assert "admin_grant" not in state


def test_challenge_cannot_be_replayed_even_after_failed_assertion(
    client, admin_user, passkey
):
    _login(client, admin_user)
    options = client.post("/admin/passkey/options", json={}).get_json()
    with client.application.test_request_context():
        origin = security.webauthn_util.origin()
    bad = passkey.sign(options, origin, uv=False)
    payload = {"request_id": options["request_id"], "credential": bad}
    assert client.post("/admin/passkey/verify", json=payload).status_code == 403
    payload["credential"] = passkey.sign(options, origin)
    assert client.post("/admin/passkey/verify", json=payload).status_code == 403


def test_other_users_credential_cannot_elevate(
    client, admin_user, regular_user, passkey
):
    other_key = SigningPasskey.register(regular_user)
    _login(client, admin_user)
    assert other_key.verify(client).status_code == 403


def test_protected_action_needs_separate_exact_one_use_proof(
    client, admin_user, passkey, tenant
):
    _login(client, admin_user)
    passkey.enter(client)
    assert client.get(_settings(tenant)).status_code == 200
    proof = _proof(client, passkey, tenant)
    db.session.refresh(tenant)
    assert tenant.name == "Other tenant"
    headers = {"X-Admin-Action": proof}
    assert (
        client.post(
            _settings(tenant), data=_data(tenant, "Tampered"), headers=headers
        ).status_code
        == 403
    )
    response = client.post(_settings(tenant), data=_data(tenant), headers=headers)
    assert response.status_code == 302
    db.session.refresh(tenant)
    assert tenant.name == "Changed tenant"
    # The successful application transaction is not the proof's replay authority.
    db.session.rollback()
    assert (
        client.post(_settings(tenant), data=_data(tenant), headers=headers).status_code
        == 403
    )


def test_verified_but_cancelled_action_does_not_mutate(
    client, admin_user, passkey, tenant
):
    _login(client, admin_user)
    passkey.enter(client)
    pending = client.post(
        _settings(tenant),
        data=_data(tenant, "secret-draft-not-for-review"),
        headers={"X-Admin-Fetch": "1"},
    ).get_json()["admin_verification"]
    assert pending["summary"]["title"] == "Protected action"
    assert tenant.slug in pending["summary"]["target"]
    assert "secret-draft-not-for-review" not in str(pending)
    assert pending["replay"] is None
    proof = passkey.verify(client, pending["request_id"]).get_json()
    assert proof["server_time"] < proof["expires_at"]
    # Cancel means discarding the proof without sending the original mutation.
    assert client.get("/admin/passkey/status").get_json()["active"] is True
    db.session.refresh(tenant)
    assert tenant.name == "Other tenant"
    row = db.session.get(AdminChallenge, proof["action_token"])
    assert row.verified_at is not None
    assert row.used_at is None
    fresh = client.post(
        _settings(tenant), data=_data(tenant), headers={"X-Admin-Fetch": "1"}
    )
    assert fresh.status_code == 428
    assert fresh.get_json()["admin_verification"]["request_id"] != proof["action_token"]
    db.session.refresh(tenant)
    assert tenant.name == "Other tenant"


def test_unverified_intent_cannot_mutate(client, admin_user, passkey, tenant):
    _login(client, admin_user)
    passkey.enter(client)
    pending = client.post(
        _settings(tenant), data=_data(tenant), headers={"X-Admin-Fetch": "1"}
    ).get_json()["admin_verification"]
    response = client.post(
        _settings(tenant),
        data=_data(tenant),
        headers={"X-Admin-Action": pending["request_id"]},
    )
    assert response.status_code == 403
    db.session.refresh(tenant)
    assert tenant.name == "Other tenant"


def test_expired_elevation_returns_entry_intent_then_fresh_action(
    client, admin_user, passkey, tenant
):
    _login(client, admin_user)
    passkey.enter(client)
    old_proof = _proof(client, passkey, tenant)
    with client.session_transaction() as state:
        old_grant_id = state["admin_grant"]
    db.session.get(AdminGrant, old_grant_id).expires_at = datetime.now(
        timezone.utc
    ).replace(tzinfo=None) - timedelta(seconds=1)
    db.session.commit()
    response = client.post(
        _settings(tenant), data=_data(tenant), headers={"X-Admin-Fetch": "1"}
    )
    assert response.status_code == 428
    assert response.get_json()["admin_verification"]["kind"] == "entry"
    renewed = passkey.verify(client).get_json()
    assert renewed["admin_access"]["active"] is True
    db.session.refresh(tenant)
    assert tenant.name == "Other tenant"
    assert (
        client.post(
            _settings(tenant), data=_data(tenant), headers={"X-Admin-Action": old_proof}
        ).status_code
        == 403
    )
    new_proof = _proof(client, passkey, tenant)
    assert new_proof != old_proof
    db.session.refresh(tenant)
    assert tenant.name == "Other tenant"
    assert (
        client.post(
            _settings(tenant), data=_data(tenant), headers={"X-Admin-Action": new_proof}
        ).status_code
        == 302
    )


def test_status_is_metadata_not_authority(client, admin_user, passkey, tenant):
    inactive = client.get("/admin/passkey/status")
    assert inactive.get_json()["active"] is False
    assert inactive.get_json()["expires_at"] is None
    assert inactive.headers["Cache-Control"] == "no-store"
    _login(client, admin_user)
    entered = passkey.verify(client).get_json()["admin_access"]
    state = client.get("/admin/passkey/status").get_json()
    assert set(state) == {"active", "expires_at", "server_time"}
    assert state["active"] is True
    assert state["expires_at"] == entered["expires_at"]
    for _ in range(2):
        assert (
            client.get("/admin/passkey/status").get_json()["expires_at"]
            == state["expires_at"]
        )
    # Display timestamps cannot replace the grant or mint an action proof.
    assert (
        client.post(
            _settings(tenant),
            data=_data(tenant),
            headers={"X-Admin-Fetch": "1", "X-Admin-Action": str(state["expires_at"])},
        ).status_code
        == 403
    )
    assert (
        client.post("/admin/passkey/end", json={}).get_json()["admin_access"]["active"]
        is False
    )
    assert client.get("/admin/passkey/status").get_json()["active"] is False
    db.session.refresh(tenant)
    assert tenant.name == "Other tenant"


def test_uploaded_bytes_and_metadata_are_bound_to_action(
    client, admin_user, passkey, tenant
):
    _login(client, admin_user)
    passkey.enter(client)

    def data(contents=b"original bytes", filename="settings.txt"):
        return {**_data(tenant), "attachment": (BytesIO(contents), filename)}

    pending = client.post(
        _settings(tenant), data=data(), headers={"X-Admin-Fetch": "1"}
    )
    assert pending.status_code == 428
    context = pending.get_json()["admin_verification"]
    assert context["replay"] is None
    proof = passkey.verify(client, context["request_id"]).get_json()["action_token"]
    headers = {"X-Admin-Action": proof}
    assert (
        client.post(
            _settings(tenant), data=data(b"changed bytes"), headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            _settings(tenant), data=data(filename="different.txt"), headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(_settings(tenant), data=data(), headers=headers).status_code == 302
    )
    assert (
        client.post(_settings(tenant), data=data(), headers=headers).status_code == 403
    )
    db.session.refresh(tenant)
    assert tenant.name == "Changed tenant"


def test_concurrent_action_submissions_consume_only_once(
    client, admin_user, passkey, tenant
):
    _login(client, admin_user)
    passkey.enter(client)
    proof = _proof(client, passkey, tenant)
    with client.session_transaction() as state:
        saved = dict(state)
    url, data = _settings(tenant), _data(tenant)
    barrier = Barrier(2)

    def submit() -> int:
        other_client = client.application.test_client()
        with other_client.session_transaction() as state:
            state.update(saved)
        barrier.wait(timeout=10)
        return other_client.post(
            url, data=data, headers={"X-Admin-Action": proof}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))
    assert sorted(results) == [302, 403]


def test_action_proof_is_bound_to_target_and_method(
    client, admin_user, passkey, tenant
):
    _login(client, admin_user)
    passkey.enter(client)
    proof = _proof(client, passkey, tenant)
    headers = {"X-Admin-Action": proof}
    response = client.post(
        f"/test/admin-action/{tenant.slug}/delete", data=_data(tenant), headers=headers
    )
    assert response.status_code == 403
    assert client.get(_settings(tenant), headers=headers).status_code == 200
    row = db.session.get(AdminChallenge, proof)
    assert row.used_at is None


@pytest.mark.parametrize(
    "revocation",
    ["expiry", "demotion", "deactivation", "rotation", "credential", "end", "logout"],
)
def test_entry_revocation_blocks_reads_and_pending_actions(
    client, admin_user, passkey, tenant, revocation
):
    _login(client, admin_user)
    passkey.enter(client)
    proof = _proof(client, passkey, tenant)
    with client.session_transaction() as state:
        original_state = dict(state)
        grant_id = state["admin_grant"]
    if revocation == "expiry":
        db.session.get(AdminGrant, grant_id).expires_at = datetime.now(
            timezone.utc
        ).replace(tzinfo=None) - timedelta(seconds=1)
    elif revocation == "demotion":
        admin_user.admin = False
    elif revocation == "deactivation":
        admin_user.active = False
    elif revocation == "rotation":
        admin_user.fs_uniquifier = uuid.uuid4().hex
    elif revocation == "credential":
        WebAuthn.query.filter_by(user_id=admin_user.id).delete()
    elif revocation == "end":
        assert client.post("/admin/passkey/end").status_code == 302
    elif revocation == "logout":
        client.get("/logout")
    db.session.commit()
    # Replaying a signed pre-logout cookie cannot revive server-side authority.
    with client.session_transaction() as state:
        state.clear()
        state.update(original_state)
    state = client.get("/admin/passkey/status").get_json()
    assert state["active"] is False
    assert state["expires_at"] is None
    assert client.get(_settings(tenant)).status_code in {302, 403}
    response = client.post(
        _settings(tenant), data=_data(tenant), headers={"X-Admin-Action": proof}
    )
    assert response.status_code in {302, 403}
    db.session.refresh(tenant)
    assert tenant.name == "Other tenant"


def test_action_expires_without_extending_entry(client, admin_user, passkey, tenant):
    _login(client, admin_user)
    passkey.enter(client)
    with client.session_transaction() as state:
        grant_id = state["admin_grant"]
    before = db.session.get(AdminGrant, grant_id).expires_at
    proof = _proof(client, passkey, tenant)
    db.session.get(AdminChallenge, proof).expires_at = datetime.now(
        timezone.utc
    ).replace(tzinfo=None) - timedelta(seconds=1)
    db.session.commit()
    assert (
        client.post(
            _settings(tenant), data=_data(tenant), headers={"X-Admin-Action": proof}
        ).status_code
        == 403
    )
    assert db.session.get(AdminGrant, grant_id).expires_at == before
    assert client.get(_settings(tenant)).status_code == 200


def test_csrf_is_required_before_ceremonies_and_writes(
    client, admin_user, passkey, tenant, monkeypatch
):
    _login(client, admin_user)
    passkey.enter(client)
    monkeypatch.setitem(client.application.config, "WTF_CSRF_ENABLED", True)
    assert client.post("/admin/passkey/options", json={}).status_code == 400
    assert client.post("/admin/passkey/verify", json={}).status_code == 400
    assert client.post("/admin/passkey/end").status_code == 400
    assert client.post(_settings(tenant), data=_data(tenant)).status_code == 400


def test_entry_ceremony_accepts_real_csrf_token(
    client, admin_user, passkey, monkeypatch
):
    monkeypatch.setitem(client.application.config, "WTF_CSRF_ENABLED", True)
    _login(client, admin_user)
    entry = client.get("/admin/passkey/")
    assert entry.status_code == 200
    token = re.search(r'data-csrf-token="([^"]+)"', entry.get_data(as_text=True))
    assert token is not None
    headers = {"X-CSRFToken": token.group(1), "Referer": entry.request.url}
    response = client.post("/admin/passkey/options", json={}, headers=headers)
    assert response.status_code == 200
    options = response.get_json()
    with client.application.test_request_context():
        origin = security.webauthn_util.origin()
    payload = {
        "request_id": options["request_id"],
        "credential": passkey.sign(options, origin),
    }
    response = client.post("/admin/passkey/verify", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["verified"] is True
    assert response.get_json()["admin_access"]["active"] is True
    with client.session_transaction() as state:
        assert db.session.get(AdminGrant, state["admin_grant"]) is not None
    assert client.get("/admin/").status_code == 200
    assert (
        client.post("/admin/passkey/verify", json=payload, headers=headers).status_code
        == 403
    )


def test_native_last_passkey_deletion_with_totp_revokes_elevation_not_login(
    client, admin_user, passkey
):
    admin_user.tf_primary_method = "authenticator"
    admin_user.tf_totp_secret = "test-secret"
    db.session.commit()
    _login(client, admin_user)
    passkey.enter(client)
    response = client.post("/wan-delete", data={"name": "Security test passkey"})
    assert response.status_code == 302
    assert WebAuthn.query.filter_by(user_id=admin_user.id).count() == 0
    assert AdminGrant.query.filter_by(user_id=admin_user.id).count() == 0
    with client.session_transaction() as state:
        assert state["_user_id"] == admin_user.fs_uniquifier
    assert client.get("/admin/").status_code == 302


def test_unverified_action_id_is_not_authority(client, admin_user, passkey, tenant):
    _login(client, admin_user)
    passkey.enter(client)
    response = client.post(
        _settings(tenant), data=_data(tenant), headers={"X-Admin-Fetch": "1"}
    )
    pending = response.get_json()["admin_verification"]["request_id"]
    assert (
        client.post(
            _settings(tenant),
            data=_data(tenant),
            headers={"X-Admin-Action": pending},
        ).status_code
        == 403
    )
    db.session.refresh(tenant)
    assert tenant.name == "Other tenant"


def test_verified_browser_replay_preserves_navigation(
    client, admin_user, passkey, tenant
):
    _login(client, admin_user)
    passkey.enter(client)
    proof = _proof(client, passkey, tenant)
    response = client.post(
        _settings(tenant),
        data=_data(tenant),
        headers={"X-Admin-Action": proof, "X-Admin-Navigate": "1"},
    )
    assert response.status_code == 200
    assert response.get_json()["admin_redirect"] == _settings(tenant)
    assert response.headers["Cache-Control"] == "no-store"
    db.session.refresh(tenant)
    assert tenant.name == "Changed tenant"


@pytest.mark.parametrize("end_access", ["end", "logout"])
def test_ending_access_invalidates_pending_entry_assertion(
    client, admin_user, passkey, end_access
):
    _login(client, admin_user)
    options = client.post("/admin/passkey/options", json={}).get_json()
    with client.application.test_request_context():
        origin = security.webauthn_util.origin()
    signed = {
        "request_id": options["request_id"],
        "credential": passkey.sign(options, origin),
    }
    with client.session_transaction() as state:
        original_cookie = dict(state)
    if end_access == "end":
        client.post("/admin/passkey/end")
    else:
        client.get("/logout")
    with client.session_transaction() as state:
        state.clear()
        state.update(original_cookie)
    g.pop("_login_user", None)
    assert client.post("/admin/passkey/verify", json=signed).status_code == 403


def test_entry_does_not_grant_native_tenant_permissions(
    client, admin_user, passkey, tenant
):
    _login(client, admin_user)
    passkey.enter(client)
    url = f"/organizations/{tenant.slug}/settings"
    assert client.get(url).status_code == 403
    assert client.post(url, data=_data(tenant)).status_code == 403
    db.session.refresh(tenant)
    assert tenant.name == "Other tenant"


def test_native_membership_does_not_require_passkey_entry(client, regular_user, tenant):
    from cabotage.server.models.auth_associations import OrganizationMember

    db.session.add(
        OrganizationMember(
            organization_id=tenant.id, user_id=regular_user.id, admin=True
        )
    )
    db.session.commit()
    _login(client, regular_user)
    url = f"/organizations/{tenant.slug}/settings"
    assert client.get(url).status_code == 200
    assert client.post(url, data=_data(tenant)).status_code == 302
    db.session.refresh(tenant)
    assert tenant.name == "Changed tenant"
    assert AdminChallenge.query.filter_by(user_id=regular_user.id).count() == 0


def test_elevated_member_form_redirect_is_returned_not_followed(
    client, admin_user, passkey, tenant
):
    from cabotage.server.models.auth_associations import OrganizationMember

    db.session.add(
        OrganizationMember(organization_id=tenant.id, user_id=admin_user.id, admin=True)
    )
    db.session.commit()
    _login(client, admin_user)
    passkey.enter(client)
    url = f"/organizations/{tenant.slug}/settings"
    # The access band submits forms with fetch; a followed redirect would fail
    # for GitHub and load same-site pages twice.
    response = client.post(url, data=_data(tenant), headers={"X-Admin-Navigate": "1"})
    assert response.status_code == 200
    assert response.get_json() == {"admin_redirect": url}
    assert response.headers["Cache-Control"] == "no-store"
    db.session.refresh(tenant)
    assert tenant.name == "Changed tenant"
    assert AdminChallenge.query.filter_by(user_id=admin_user.id).count() == 0
