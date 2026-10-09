"""Behavioral boundaries use real ES256 signatures, never a stubbed verifier."""

import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urlencode
from unittest.mock import Mock

import pytest
from flask import g, session
from sqlalchemy import delete, event, update

from cabotage.server import db, security
from cabotage.server.admin_passkey import (
    consume_shell_ticket,
    elevated_stream_valid,
    has_admin_session,
    issue_shell_ticket,
)
from cabotage.server.models.admin_security import AdminChallenge, AdminGrant
from cabotage.server.models.auth import (
    DiscordIntegration,
    Organization,
    OrganizationRequest,
    SlackIntegration,
    User,
    WebAuthn,
)
from cabotage.server.models.auth_associations import OrganizationMember
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
    from cabotage.server.integrations.discord_oauth import discord_oauth_bp
    from cabotage.server.integrations.slack_oauth import slack_oauth_bp

    # Settings expose the OIDC issuer, which must use HTTPS outside debug mode.
    monkeypatch.setitem(app.config, "EXT_PREFERRED_URL_SCHEME", "https")
    # Optional OAuth providers are absent from the shared app's startup config.
    # Register their real routes before requests, as the provider tests do.
    with monkeypatch.context() as setup:
        setup.setattr(app, "_got_first_request", False)
        for blueprint in (slack_oauth_bp, discord_oauth_bp):
            if blueprint.name not in app.blueprints:
                app.register_blueprint(blueprint)


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
def stream_clock(monkeypatch):
    from cabotage.server import admin_passkey

    clock = {"wall": admin_passkey._now(), "monotonic": 0.0}
    monkeypatch.setattr(admin_passkey, "_now", lambda: clock["wall"])
    monkeypatch.setattr(admin_passkey, "monotonic", lambda: clock["monotonic"])
    return clock


@pytest.fixture
def stream_grant_queries(app):
    queries = []
    engine = db.engine

    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "admin_grants" in statement:
            queries.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    yield queries
    event.remove(engine, "before_cursor_execute", record)


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
    return f"/organizations/{tenant.slug}/settings"


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
    assert client.get(_settings(tenant)).status_code == 403
    assert client.get("/admin/db/").status_code == 302
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


def test_cross_tenant_write_needs_separate_exact_one_use_proof(
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
    assert pending["summary"]["title"] == "Update organization"
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


@pytest.mark.parametrize("action", ["approve", "deny"])
def test_request_review_names_proposed_organization_and_requester(
    client, admin_user, regular_user, passkey, monkeypatch, action
):
    monkeypatch.setitem(
        client.application.config, "ORGANIZATION_REQUESTS_ENABLED", True
    )
    org_request = OrganizationRequest(
        requester_user_id=regular_user.id,
        name="Proposed organization",
        slug=f"review-{uuid.uuid4().hex[:8]}",
        note="private-note-not-for-impact-review",
    )
    db.session.add(org_request)
    db.session.commit()
    _login(client, admin_user)
    passkey.enter(client)
    response = client.post(
        f"/organization-requests/{org_request.id}/{action}",
        headers={"X-Admin-Fetch": "1"},
    )
    assert response.status_code == 428
    context = response.get_json()["admin_verification"]
    summary = context["summary"]
    assert summary["title"] == f"{action.capitalize()} organization request"
    assert org_request.name in summary["target"]
    assert org_request.slug in summary["target"]
    assert regular_user.username in summary["target"]
    assert str(org_request.id) in summary["target"]
    assert org_request.note not in str(context)
    if action == "approve":
        assert "organization admin access" in summary["consequence"]
    else:
        assert "No organization or membership will be created" in summary["consequence"]
    assert passkey.verify(client, context["request_id"]).status_code == 200
    db.session.refresh(org_request)
    assert org_request.is_pending
    assert Organization.query.filter_by(slug=org_request.slug).first() is None


def test_add_member_review_only_names_resolved_accounts(
    client, admin_user, regular_user, passkey, tenant
):
    _login(client, admin_user)
    passkey.enter(client)
    for identity in (regular_user.email, "unknown-secret-input"):
        response = client.post(
            f"/organizations/{tenant.slug}/users/add",
            data={"identity": identity},
            headers={"X-Admin-Fetch": "1"},
        )
        assert response.status_code == 428
        context = response.get_json()["admin_verification"]
        assert tenant.slug in context["summary"]["target"]
        if identity == regular_user.email:
            assert regular_user.username in context["summary"]["target"]
            assert regular_user.email in context["summary"]["target"]
        else:
            assert identity not in str(context)
    assert (
        OrganizationMember.query.filter_by(
            organization_id=tenant.id, user_id=regular_user.id
        ).first()
        is None
    )


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


@pytest.mark.parametrize("member", [False, True])
def test_expired_elevation_retains_membership_precedence(
    client, admin_user, passkey, tenant, member
):
    if member:
        db.session.add(
            OrganizationMember(
                user_id=admin_user.id, organization_id=tenant.id, admin=True
            )
        )
        db.session.commit()
    _login(client, admin_user)
    passkey.enter(client)
    with client.session_transaction() as state:
        grant_id = state["admin_grant"]
    db.session.get(AdminGrant, grant_id).expires_at = datetime.now(
        timezone.utc
    ).replace(tzinfo=None) - timedelta(seconds=1)
    db.session.commit()
    response = client.post(
        _settings(tenant), data=_data(tenant), headers={"X-Admin-Fetch": "1"}
    )
    assert response.status_code == (302 if member else 428)
    db.session.refresh(tenant)
    assert tenant.name == ("Changed tenant" if member else "Other tenant")


@pytest.mark.parametrize("global_admin", [False, True])
def test_no_prior_elevation_is_not_an_inplace_entry_hint(
    client, admin_user, regular_user, tenant, global_admin
):
    _login(client, admin_user if global_admin else regular_user)
    response = client.post(
        _settings(tenant), data=_data(tenant), headers={"X-Admin-Fetch": "1"}
    )
    assert response.status_code == 403
    assert "admin_verification" not in response.get_data(as_text=True)


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
        f"/organizations/{tenant.slug}/delete", data=_data(tenant), headers=headers
    )
    assert response.status_code == 403
    assert client.get(_settings(tenant), headers=headers).status_code == 200
    row = db.session.get(AdminChallenge, proof)
    assert row.used_at is None


def test_membership_behavior_does_not_require_elevation(client, regular_user, tenant):
    db.session.add(
        OrganizationMember(
            user_id=regular_user.id, organization_id=tenant.id, admin=True
        )
    )
    db.session.commit()
    _login(client, regular_user)
    assert client.post(_settings(tenant), data=_data(tenant)).status_code == 302
    db.session.refresh(tenant)
    assert tenant.name == "Changed tenant"
    assert AdminChallenge.query.filter_by(user_id=regular_user.id).count() == 0


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


@pytest.mark.parametrize("provider", ["slack", "discord"])
def test_elevated_oauth_get_stages_post_without_consuming_state(
    client, admin_user, passkey, tenant, provider
):
    _login(client, admin_user)
    passkey.enter(client)
    with client.session_transaction() as state:
        state[f"{provider}_oauth_state"] = "bound-state"
        state[f"{provider}_oauth_org_slug"] = tenant.slug
    response = client.get(
        f"/integrations/{provider}/callback?state=bound-state&code=provider-code",
        headers={"X-Admin-Fetch": "1"},
    )
    assert response.status_code == 428
    context = response.get_json()["admin_verification"]
    assert context["request_id"] is None
    assert context["replay"]["method"] == "POST"
    with client.session_transaction() as state:
        assert state[f"{provider}_oauth_state"] == "bound-state"
        assert state[f"{provider}_oauth_org_slug"] == tenant.slug
    assert tenant.discord_integration is None
    assert tenant.slack_integration is None


@pytest.mark.parametrize("provider", ["slack", "discord"])
def test_elevated_oauth_completion_consumes_state_after_real_assertion(
    client, admin_user, passkey, tenant, monkeypatch, provider
):
    from cabotage.server.integrations import discord_oauth, slack_oauth

    _login(client, admin_user)
    passkey.enter(client)
    integration_module = slack_oauth if provider == "slack" else discord_oauth
    monkeypatch.setitem(
        client.application.config, f"{provider.upper()}_CLIENT_ID", "test-client"
    )
    monkeypatch.setitem(
        client.application.config, f"{provider.upper()}_CLIENT_SECRET", "test-secret"
    )
    if provider == "slack":
        data = {
            "ok": True,
            "access_token": "test-token",
            "team": {"id": "123", "name": "Test workspace"},
            "bot_user_id": "456",
        }
        monkeypatch.setattr(
            slack_oauth, "vault", Mock(vault_prefix="test", vault_connection=Mock())
        )
    else:
        data = {"guild": {"id": "123", "name": "Test guild"}}
    exchange = Mock(return_value=Mock(json=lambda: data))
    monkeypatch.setattr(integration_module.http_requests, "post", exchange)
    with client.session_transaction() as state:
        state[f"{provider}_oauth_state"] = "bound-state"
        state[f"{provider}_oauth_org_slug"] = tenant.slug
    url = f"/integrations/{provider}/callback?" + urlencode(
        {"state": "bound-state", "code": "provider-code"}
    )
    pending = client.post(url, headers={"X-Admin-Fetch": "1"})
    assert pending.status_code == 428
    exchange.assert_not_called()
    with client.session_transaction() as state:
        assert state[f"{provider}_oauth_state"] == "bound-state"
        assert state[f"{provider}_oauth_org_slug"] == tenant.slug
    proof = passkey.verify(
        client, pending.get_json()["admin_verification"]["request_id"]
    )
    assert proof.status_code == 200
    headers = {"X-Admin-Action": proof.get_json()["action_token"]}
    with client.session_transaction() as state:
        original_cookie = dict(state)
        assert state[f"{provider}_oauth_state"] == "bound-state"
        assert state[f"{provider}_oauth_org_slug"] == tenant.slug
    response = client.post(url, headers=headers)
    assert response.status_code == 302
    assert exchange.call_count == 1
    if provider == "slack":
        assert (
            SlackIntegration.query.filter_by(organization_id=tenant.id).one().team_id
            == "123"
        )
    else:
        assert (
            DiscordIntegration.query.filter_by(organization_id=tenant.id).one().guild_id
            == "123"
        )
    with client.session_transaction() as state:
        assert f"{provider}_oauth_state" not in state
        assert f"{provider}_oauth_org_slug" not in state
    assert client.post(url).status_code == 302
    assert exchange.call_count == 1
    with client.session_transaction() as state:
        state.clear()
        state.update(original_cookie)
    assert client.post(url, headers=headers).status_code == 403
    assert exchange.call_count == 1


@pytest.mark.parametrize("provider", ["slack", "discord"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_oauth_permission_rejection_consumes_state(
    client, regular_user, tenant, monkeypatch, provider, method
):
    from cabotage.server.integrations import discord_oauth, slack_oauth

    _login(client, regular_user)
    integration_module = slack_oauth if provider == "slack" else discord_oauth
    exchange = Mock()
    monkeypatch.setattr(integration_module.http_requests, "post", exchange)
    with client.session_transaction() as state:
        state[f"{provider}_oauth_state"] = "bound-state"
        state[f"{provider}_oauth_org_slug"] = tenant.slug
    url = f"/integrations/{provider}/callback?state=bound-state&code=provider-code"
    assert client.open(url, method=method).status_code == 403
    with client.session_transaction() as state:
        assert f"{provider}_oauth_state" not in state
        assert f"{provider}_oauth_org_slug" not in state
    exchange.assert_not_called()
    assert tenant.slack_integration is None
    assert tenant.discord_integration is None

    # Restoring permission must not make the rejected callback reusable.
    db.session.add(
        OrganizationMember(
            user_id=regular_user.id, organization_id=tenant.id, admin=True
        )
    )
    db.session.commit()
    assert client.open(url, method=method).status_code == 302
    exchange.assert_not_called()


def test_elevated_github_callback_stages_before_token_exchange(
    client, admin_user, passkey, tenant, monkeypatch
):
    from cabotage.server.user import github_installations, github_oauth

    application = client.application
    if "github_oauth" not in application.blueprints:
        application._got_first_request = False
        application.register_blueprint(github_oauth.github_oauth_bp)
    _login(client, admin_user)
    passkey.enter(client)
    exchange = Mock(side_effect=AssertionError("GET must not exchange the OAuth code"))
    monkeypatch.setattr(github_oauth, "_fetch_github_user_access_token", exchange)
    state = github_installations.connect_state(
        tenant, admin_user.id, installation_id=123
    )
    response = client.get(
        "/auth/github/callback?" + urlencode({"state": state, "code": "provider-code"}),
        headers={"X-Admin-Fetch": "1"},
    )
    assert response.status_code == 428
    assert response.get_json()["admin_verification"]["replay"]["method"] == "POST"
    exchange.assert_not_called()


def test_elevated_stream_hot_loop_bounds_grant_queries(
    client, admin_user, passkey, stream_clock, stream_grant_queries
):
    _login(client, admin_user)
    passkey.enter(client)
    with client.session_transaction() as state:
        saved = dict(state)
    with client.application.test_request_context():
        session.update(saved)
        g.admin_elevated = True
        stream_grant_queries.clear()
        for second in range(3):
            for tick in range(1000):
                stream_clock["monotonic"] = second + tick / 1000
                assert elevated_stream_valid()
            assert len(stream_grant_queries) == second + 1


@pytest.mark.parametrize(
    "revocation", ["grant", "demotion", "deactivation", "rotation", "credential"]
)
def test_elevated_stream_detects_revocation_at_one_second(
    client, admin_user, passkey, stream_clock, stream_grant_queries, revocation
):
    _login(client, admin_user)
    passkey.enter(client)
    with client.session_transaction() as state:
        saved = dict(state)
    with client.application.test_request_context():
        session.update(saved)
        g.admin_elevated = True
        stream_grant_queries.clear()
        assert elevated_stream_valid()
        # Revoke in a separate transaction, as another request would.
        with db.engine.begin() as connection:
            if revocation == "grant":
                connection.execute(
                    delete(AdminGrant).where(AdminGrant.user_id == admin_user.id)
                )
            elif revocation == "credential":
                connection.execute(
                    delete(WebAuthn).where(WebAuthn.user_id == admin_user.id)
                )
            else:
                changes = {
                    "demotion": {"admin": False},
                    "deactivation": {"active": False},
                    "rotation": {"fs_uniquifier": uuid.uuid4().hex},
                }
                connection.execute(
                    update(User)
                    .where(User.id == admin_user.id)
                    .values(**changes[revocation])
                )
        stream_clock["monotonic"] = 0.999
        assert elevated_stream_valid()
        assert len(stream_grant_queries) == 1
        stream_clock["monotonic"] = 1.0
        assert not elevated_stream_valid()
        assert not elevated_stream_valid()
        assert len(stream_grant_queries) == 2


def test_elevated_stream_checks_absolute_expiry_between_revocation_checks(
    client, admin_user, passkey, stream_clock, stream_grant_queries
):
    _login(client, admin_user)
    passkey.enter(client)
    with client.session_transaction() as state:
        saved = dict(state)
    expires_at = stream_clock["wall"] + timedelta(milliseconds=250)
    db.session.get(AdminGrant, saved["admin_grant"]).expires_at = expires_at
    db.session.commit()
    with client.application.test_request_context():
        session.update(saved)
        g.admin_elevated = True
        stream_grant_queries.clear()
        assert elevated_stream_valid()
        stream_clock["monotonic"] = 0.249
        stream_clock["wall"] = expires_at - timedelta(milliseconds=1)
        assert elevated_stream_valid()
        stream_clock["monotonic"] = 0.250
        stream_clock["wall"] = expires_at
        assert not elevated_stream_valid()
        stream_clock["monotonic"] = 0.500
        stream_clock["wall"] += timedelta(milliseconds=250)
        assert not elevated_stream_valid()
        assert len(stream_grant_queries) == 1


@pytest.mark.parametrize("change", ["grant", "binding", "user", "uniquifier", "logout"])
def test_elevated_stream_does_not_reuse_another_identity_check(
    client,
    admin_user,
    regular_user,
    passkey,
    stream_clock,
    stream_grant_queries,
    change,
):
    _login(client, admin_user)
    passkey.enter(client)
    with client.session_transaction() as state:
        saved = dict(state)
    with client.application.test_request_context():
        session.update(saved)
        g.admin_elevated = True
        stream_grant_queries.clear()
        assert elevated_stream_valid()
        if change == "grant":
            session["admin_grant"] = "another-grant"
        elif change == "binding":
            session["admin_binding"] = "another-binding"
        elif change == "user":
            session["_user_id"] = regular_user.fs_uniquifier
            g.pop("_login_user", None)
        elif change == "uniquifier":
            g._login_user.fs_uniquifier = uuid.uuid4().hex
            db.session.commit()
        else:
            session.pop("_user_id")
            g.pop("_login_user", None)
        assert not elevated_stream_valid()
        assert len(stream_grant_queries) == (1 if change == "logout" else 2)


def test_elevated_stream_cache_is_request_local(
    client, admin_user, passkey, stream_clock, stream_grant_queries
):
    _login(client, admin_user)
    passkey.enter(client)
    with client.session_transaction() as state:
        saved = dict(state)
    stream_grant_queries.clear()
    with client.application.test_request_context():
        session.update(saved)
        g.admin_elevated = True
        assert elevated_stream_valid()
    db.session.execute(delete(AdminGrant).where(AdminGrant.user_id == admin_user.id))
    db.session.commit()
    with client.application.test_request_context():
        session.update(saved)
        g.admin_elevated = True
        assert not elevated_stream_valid()
    assert len(stream_grant_queries) == 2


def test_ordinary_member_stream_never_queries_admin_grants(
    client, regular_user, stream_clock, stream_grant_queries
):
    _login(client, regular_user)
    with client.session_transaction() as state:
        saved = dict(state)
    with client.application.test_request_context():
        session.update(saved)
        g.admin_elevated = False
        stream_grant_queries.clear()
        for second in range(3):
            stream_clock["monotonic"] = second
            assert elevated_stream_valid()
        assert not stream_grant_queries


def test_live_log_idle_tick_closes_after_revocation(
    client, admin_user, passkey, monkeypatch, stream_clock, stream_grant_queries
):
    from cabotage.server.user import views

    _login(client, admin_user)
    passkey.enter(client)
    with client.session_transaction() as state:
        saved = dict(state)
    socket = Mock()
    monkeypatch.setattr(views, "get_redis_client", lambda _: object())

    def lines(*args):
        db.session.execute(
            delete(AdminGrant).where(AdminGrant.user_id == admin_user.id)
        )
        db.session.commit()
        stream_clock["monotonic"] = 0.999
        yield None
        socket.close.assert_not_called()
        stream_clock["monotonic"] = 1.0
        yield None
        yield "must never leave the server"

    monkeypatch.setattr(views, "read_log_stream", lines)
    with client.application.test_request_context():
        session.update(saved)
        g.admin_elevated = True
        stream_grant_queries.clear()
        assert elevated_stream_valid()
        views._stream_redis_build_logs(socket, "image", "job", "test")
    socket.close.assert_called_once()
    assert all("must never" not in str(call) for call in socket.send.call_args_list)
    assert len(stream_grant_queries) == 2


def test_shell_capability_is_one_use_and_entry_bound(client, admin_user, passkey):
    _login(client, admin_user)
    passkey.enter(client)
    with client.session_transaction() as state:
        saved = dict(state)
    with client.application.test_request_context("/shell", method="POST"):
        session.update(saved)
        g.admin_elevated = True
        g.admin_action_digest = "already-consumed-request-proof"
        issue_shell_ticket("/shell/socket")
        saved = dict(session)
    with client.application.test_request_context("/shell/socket"):
        origin = security.webauthn_util.origin()
    with client.application.test_request_context(
        "/shell/socket", headers={"Origin": origin}
    ):
        session.update(saved)
        assert has_admin_session()
        assert consume_shell_ticket()
        assert not consume_shell_ticket()
    db.session.execute(delete(AdminGrant).where(AdminGrant.user_id == admin_user.id))
    db.session.commit()
    with client.application.test_request_context(
        "/shell/socket", headers={"Origin": origin}
    ):
        session.update(saved)
        assert not has_admin_session()
        assert not consume_shell_ticket()


@pytest.mark.parametrize(
    "suffix,data",
    [
        ("active", {"active": "false"}),
        ("admin", {"admin": "false"}),
        ("mfa-reset", None),
    ],
)
def test_console_refuses_self_lockout(client, admin_user, passkey, suffix, data):
    _login(client, admin_user)
    passkey.enter(client)
    old_uniquifier = admin_user.fs_uniquifier
    response = client.post(
        f"/admin/users/{admin_user.id}/{suffix}",
        data=data or {"confirm": admin_user.username},
    )
    assert response.status_code == 302
    db.session.refresh(admin_user)
    assert admin_user.active and admin_user.admin
    assert admin_user.fs_uniquifier == old_uniquifier
    assert WebAuthn.query.filter_by(user_id=admin_user.id).count() == 1


def test_console_deactivation_requires_fresh_proof_and_rotates_login(
    client, admin_user, regular_user, passkey
):
    _login(client, admin_user)
    passkey.enter(client)
    before = regular_user.fs_uniquifier
    response = passkey.post(
        client, f"/admin/users/{regular_user.id}/active", {"active": "false"}
    )
    assert response.status_code == 302
    db.session.refresh(regular_user)
    assert not regular_user.active
    assert regular_user.fs_uniquifier != before


def test_console_mfa_reset_revokes_keys_sessions_and_entry(
    client, admin_user, regular_user, passkey
):
    regular_user.admin = True
    regular_user.tf_primary_method = "authenticator"
    regular_user.tf_totp_secret = "test-secret"
    regular_user.mf_recovery_codes = ["test-recovery"]
    target_key = SigningPasskey.register(regular_user)
    target_client = client.application.test_client()
    _login(target_client, regular_user)
    target_key.enter(target_client)
    before, password = regular_user.fs_uniquifier, regular_user.password
    _login(client, admin_user)
    passkey.enter(client)
    response = passkey.post(
        client,
        f"/admin/users/{regular_user.id}/mfa-reset",
        {"confirm": regular_user.username},
    )
    assert response.status_code == 302
    db.session.refresh(regular_user)
    assert regular_user.fs_uniquifier != before
    assert regular_user.password == password
    assert not regular_user.tf_primary_method
    assert not regular_user.tf_totp_secret
    assert not regular_user.mf_recovery_codes
    assert WebAuthn.query.filter_by(user_id=regular_user.id).count() == 0
    assert AdminGrant.query.filter_by(user_id=regular_user.id).count() == 0


@pytest.mark.parametrize("change", ["deactivate", "demote", "reset_mfa"])
def test_last_usable_admin_refusal_excludes_keyless_admins(
    admin_user, regular_user, passkey, change
):
    from cabotage.server.admin_console.accounts import _usable_admin_ids, refusal

    regular_user.admin = True
    db.session.commit()
    assert admin_user.id in _usable_admin_ids()
    assert regular_user.id not in _usable_admin_ids()
    assert refusal(change, admin_user, regular_user.id, {admin_user.id}) is not None
    assert (
        refusal(change, admin_user, regular_user.id, {admin_user.id, regular_user.id})
        is None
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
