"""Tests for activity tracking on auth events and deletions."""

import uuid

import pytest
from flask_security import (
    hash_password,
    login_user,
    logout_user,
    tf_code_confirmed,
    tf_disabled,
    wan_registered,
    wan_deleted,
)

from cabotage.server import db
from cabotage.server.models.auth import User
from cabotage.server.models.projects import activity_plugin
from cabotage.server.wsgi import app as _app

Activity = activity_plugin.activity_cls


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["SECURITY_TWO_FACTOR_ALWAYS_VALIDATE"] = True
    with _app.app_context():
        yield _app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def test_user(app):
    u = User(
        username=f"acttest-{uuid.uuid4().hex[:8]}",
        email=f"acttest-{uuid.uuid4().hex[:8]}@example.com",
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


def _do_login(app, client, user):
    """Login using Flask-Security's login_user to ensure signals fire."""
    with client.session_transaction():
        pass  # ensure session exists
    login_user(user)


def _get_activities(user, verb=None):
    q = db.session.query(Activity).filter(
        Activity.object_id == user.id,
    )
    if verb:
        q = q.filter(Activity.verb == verb)
    return q.order_by(Activity.id.desc()).all()


def _send_signal_in_request(client, test_user, signal):
    """Fire a signal during a real request and trigger after_request."""
    with client:
        client.get("/")
        login_user(test_user)
        # Send signal while in request context, then make another
        # request so after_request flushes the queue
        signal.send(client.application, user=test_user)
        client.get("/")


class TestLoginActivity:
    def test_login_creates_activity(self, app, client, test_user):
        with client:
            client.get("/")
            login_user(test_user)
            # Trigger after_request by making another request
            client.get("/")
        activities = _get_activities(test_user, verb="login")
        assert len(activities) >= 1
        assert activities[0].data["user_id"] == str(test_user.id)

    def test_login_activity_has_timestamp(self, app, client, test_user):
        with client:
            client.get("/")
            login_user(test_user)
            client.get("/")
        activities = _get_activities(test_user, verb="login")
        assert activities[0].data.get("timestamp")


class TestLogoutActivity:
    def test_logout_creates_activity(self, app, client, test_user):
        with client:
            client.get("/")
            login_user(test_user)
            # Logout triggers signal, next request flushes the queue
            logout_user()
            client.get("/")
        activities = _get_activities(test_user, verb="logout")
        assert len(activities) >= 1
        assert activities[0].data["user_id"] == str(test_user.id)


class TestRecoveryCodeActivity:
    def test_recovery_code_use_creates_activity(self, app, client, test_user):
        with client:
            client.get("/")
            login_user(test_user)
            code = test_user.mf_recovery_codes[0]
            client.post(
                "/account/security/verify-recovery-code",
                data={"code": code},
            )
        activities = _get_activities(test_user, verb="edit")
        recovery_activities = [
            a
            for a in activities
            if (a.data or {}).get("action") == "recovery_code_used"
        ]
        assert len(recovery_activities) >= 1


class TestTOTPActivity:
    def test_totp_setup_creates_activity(self, app, client, test_user):
        """tf_code_confirmed signal queues a totp_setup activity."""
        _send_signal_in_request(client, test_user, tf_code_confirmed)
        activities = _get_activities(test_user, verb="edit")
        totp_activities = [
            a for a in activities if (a.data or {}).get("action") == "totp_setup"
        ]
        assert len(totp_activities) >= 1
        assert totp_activities[0].data["user_id"] == str(test_user.id)

    def test_totp_disabled_creates_activity(self, app, client, test_user):
        """tf_disabled signal queues a totp_disabled activity."""
        _send_signal_in_request(client, test_user, tf_disabled)
        activities = _get_activities(test_user, verb="edit")
        disabled_activities = [
            a for a in activities if (a.data or {}).get("action") == "totp_disabled"
        ]
        assert len(disabled_activities) >= 1


class TestWebAuthnActivity:
    def test_webauthn_registered_creates_activity(self, app, client, test_user):
        """wan_registered signal queues a webauthn_registered activity."""
        _send_signal_in_request(client, test_user, wan_registered)
        activities = _get_activities(test_user, verb="edit")
        wan_activities = [
            a
            for a in activities
            if (a.data or {}).get("action") == "webauthn_registered"
        ]
        assert len(wan_activities) >= 1

    def test_webauthn_deleted_creates_activity(self, app, client, test_user):
        """wan_deleted signal queues a webauthn_deleted activity."""
        _send_signal_in_request(client, test_user, wan_deleted)
        activities = _get_activities(test_user, verb="edit")
        del_activities = [
            a for a in activities if (a.data or {}).get("action") == "webauthn_deleted"
        ]
        assert len(del_activities) >= 1
