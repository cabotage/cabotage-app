"""Tests for MFA enforcement guards."""

import uuid
import time

import pytest  # noqa: F401 (used by fixtures)
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import User, WebAuthn, GitHubIdentity
from cabotage.server.wsgi import app as _app


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["SECURITY_TWO_FACTOR_ALWAYS_VALIDATE"] = True  # simplify tests
    with _app.app_context():
        yield _app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def user_no_mfa(app):
    """User with no MFA configured."""
    u = User(
        username=f"testuser-{uuid.uuid4().hex[:8]}",
        email=f"test-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db.session.add(u)
    db.session.commit()
    yield u
    # Clean up transaction records that reference this user (from continuum)
    db.session.execute(
        db.text("DELETE FROM activity WHERE object_id = :uid"),
        {"uid": u.id},
    )
    db.session.execute(
        db.text("UPDATE transaction SET user_id = NULL WHERE user_id = :uid"),
        {"uid": u.id},
    )
    db.session.delete(u)
    db.session.commit()


@pytest.fixture
def user_with_totp(app):
    """User with TOTP configured but no recovery codes."""
    u = User(
        username=f"testtotp-{uuid.uuid4().hex[:8]}",
        email=f"totp-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
        tf_primary_method="authenticator",
        tf_totp_secret="fakesecret",
    )
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.execute(
        db.text("DELETE FROM activity WHERE object_id = :uid"),
        {"uid": u.id},
    )
    db.session.execute(
        db.text("UPDATE transaction SET user_id = NULL WHERE user_id = :uid"),
        {"uid": u.id},
    )
    db.session.delete(u)
    db.session.commit()


@pytest.fixture
def user_fully_setup(app):
    """User with TOTP + recovery codes."""
    u = User(
        username=f"testfull-{uuid.uuid4().hex[:8]}",
        email=f"full-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
        tf_primary_method="authenticator",
        tf_totp_secret="fakesecret",
        mf_recovery_codes=["aaaa-bbbb-cccc", "dddd-eeee-ffff"],
    )
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.execute(
        db.text("DELETE FROM activity WHERE object_id = :uid"),
        {"uid": u.id},
    )
    db.session.execute(
        db.text("UPDATE transaction SET user_id = NULL WHERE user_id = :uid"),
        {"uid": u.id},
    )
    db.session.delete(u)
    db.session.commit()


@pytest.fixture
def user_with_webauthn(app):
    """User with one WebAuthn key + recovery codes."""
    u = User(
        username=f"testwan-{uuid.uuid4().hex[:8]}",
        email=f"wan-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
        fs_webauthn_user_handle=uuid.uuid4().hex,
        mf_recovery_codes=["aaaa-bbbb-cccc", "dddd-eeee-ffff"],
    )
    db.session.add(u)
    db.session.flush()

    wan = WebAuthn(
        user_id=u.id,
        credential_id=uuid.uuid4().bytes,
        public_key=b"fakepubkey",
        sign_count=0,
        backup_state=False,
        device_type="single_device",
        lastuse_datetime=db.func.now(),
        name="TestKey",
        usage="secondary",
    )
    db.session.add(wan)
    db.session.commit()
    yield u
    WebAuthn.query.filter_by(user_id=u.id).delete()
    db.session.delete(u)
    db.session.commit()


@pytest.fixture
def github_user(app):
    """User with GitHub identity + WebAuthn key + recovery codes."""
    u = User(
        username=f"github:999:{uuid.uuid4().hex[:8]}",
        email=f"gh-{uuid.uuid4().hex[:8]}@example.com",
        password="!",
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
        fs_webauthn_user_handle=uuid.uuid4().hex,
        mf_recovery_codes=["aaaa-bbbb-cccc"],
    )
    db.session.add(u)
    db.session.flush()

    gi = GitHubIdentity(
        user_id=u.id,
        github_id=999999 + hash(u.username) % 100000,
        github_username=u.username.split(":")[-1],
    )
    db.session.add(gi)

    wan = WebAuthn(
        user_id=u.id,
        credential_id=uuid.uuid4().bytes,
        public_key=b"fakepubkey",
        sign_count=0,
        backup_state=False,
        device_type="multi_device",
        lastuse_datetime=db.func.now(),
        name="Passkey",
        usage="secondary",
    )
    db.session.add(wan)
    db.session.commit()
    yield u
    WebAuthn.query.filter_by(user_id=u.id).delete()
    GitHubIdentity.query.filter_by(user_id=u.id).delete()
    db.session.delete(u)
    db.session.commit()


def _login(client, user):
    """Log a user in via the test client."""
    with client.session_transaction() as sess:
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()


# --- Guard Tests ---


class TestRequireMfaSetup:
    def test_unauthenticated_user_not_blocked(self, client):
        resp = client.get("/")
        # Should not redirect to tf-setup, should be login or home
        assert resp.status_code != 500
        assert "/tf-setup" not in (resp.headers.get("Location") or "")

    def test_user_without_mfa_redirected_to_setup(self, client, user_no_mfa):
        _login(client, user_no_mfa)
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/tf-setup" in resp.headers["Location"]

    def test_user_without_mfa_can_access_setup_endpoints(self, client, user_no_mfa):
        _login(client, user_no_mfa)
        resp = client.get("/tf-setup")
        assert resp.status_code == 200

    def test_user_with_mfa_no_recovery_codes_redirected(self, client, user_with_totp):
        _login(client, user_with_totp)
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/mf-recovery-codes" in resp.headers["Location"]

    def test_user_with_mfa_and_codes_passes(self, client, user_fully_setup):
        _login(client, user_fully_setup)
        resp = client.get("/")
        assert resp.status_code != 302 or "/tf-setup" not in resp.headers.get(
            "Location", ""
        )
        assert "/mf-recovery-codes" not in (resp.headers.get("Location") or "")


class TestGuardLastMfaMethod:
    def test_cannot_delete_last_webauthn_key(self, client, user_with_webauthn):
        _login(client, user_with_webauthn)
        resp = client.post("/wan-delete", data={"name": "TestKey"})
        assert resp.status_code == 403

    def test_cannot_disable_totp_when_only_method(self, client, user_fully_setup):
        _login(client, user_fully_setup)
        resp = client.post("/tf-setup", data={"setup": "disable"})
        assert resp.status_code == 403

    def test_can_delete_key_when_multiple_methods(self, client, app):
        """User with TOTP + WebAuthn can delete a key."""
        u = User(
            username=f"testmulti-{uuid.uuid4().hex[:8]}",
            email=f"multi-{uuid.uuid4().hex[:8]}@example.com",
            password=hash_password("password123"),
            active=True,
            fs_uniquifier=uuid.uuid4().hex,
            fs_webauthn_user_handle=uuid.uuid4().hex,
            tf_primary_method="authenticator",
            tf_totp_secret="fakesecret",
            mf_recovery_codes=["aaaa-bbbb-cccc"],
        )
        db.session.add(u)
        db.session.flush()
        wan = WebAuthn(
            user_id=u.id,
            credential_id=uuid.uuid4().bytes,
            public_key=b"fakepubkey",
            sign_count=0,
            backup_state=False,
            device_type="single_device",
            lastuse_datetime=db.func.now(),
            name="TestKey2",
            usage="secondary",
        )
        db.session.add(wan)
        db.session.commit()

        _login(client, u)
        resp = client.post("/wan-delete", data={"name": "TestKey2"})
        # Should NOT be 403 — multiple methods exist
        assert resp.status_code != 403

        WebAuthn.query.filter_by(user_id=u.id).delete()
        db.session.execute(
            db.text("DELETE FROM activity WHERE object_id = :uid"),
            {"uid": u.id},
        )
        db.session.execute(
            db.text("UPDATE transaction SET user_id = NULL WHERE user_id = :uid"),
            {"uid": u.id},
        )
        db.session.delete(u)
        db.session.commit()


class TestGitHubPasswordBlock:
    def test_github_user_blocked_from_change_password(self, client, github_user):
        _login(client, github_user)
        resp = client.get("/change")
        assert resp.status_code == 403

    def test_github_user_password_reset_sends_custom_email(
        self, client, app, github_user
    ):
        with app.extensions.get("mail", app).record_messages() as outbox:
            resp = client.post("/reset", data={"email": github_user.email})
            assert resp.status_code == 302
            # Check that a custom email was sent (not a reset link)
            github_emails = [m for m in outbox if "GitHub" in m.subject]
            assert len(github_emails) >= 1


class TestRecoveryCodeVerification:
    def test_recovery_code_verification_burns_code(self, client, user_fully_setup):
        _login(client, user_fully_setup)
        original_count = len(user_fully_setup.mf_recovery_codes)
        code = user_fully_setup.mf_recovery_codes[0]

        resp = client.post(
            "/account/security/verify-recovery-code",
            data={"code": code},
        )
        assert resp.status_code == 302

        db.session.refresh(user_fully_setup)
        assert len(user_fully_setup.mf_recovery_codes) == original_count - 1
        assert code not in user_fully_setup.mf_recovery_codes

    def test_recovery_code_verification_rejects_invalid(self, client, user_fully_setup):
        _login(client, user_fully_setup)
        resp = client.post(
            "/account/security/verify-recovery-code",
            data={"code": "xxxx-xxxx-xxxx"},
        )
        assert resp.status_code == 302
        assert "/mf-recovery-codes" in resp.headers["Location"]
