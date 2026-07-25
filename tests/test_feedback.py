import time
import uuid

import pytest
from flask_security import hash_password

from cabotage.celery.tasks import notify as notify_tasks
from cabotage.server import db
from cabotage.server.models.auth import User
from cabotage.server.models.feedback import Feedback
from cabotage.server.wsgi import app as _app


@pytest.fixture
def app():
    original_config = {
        "TESTING": _app.config.get("TESTING"),
        "WTF_CSRF_ENABLED": _app.config.get("WTF_CSRF_ENABLED"),
        "REQUIRE_MFA": _app.config.get("REQUIRE_MFA"),
        "FEEDBACK_WIDGET_ENABLED": _app.config.get("FEEDBACK_WIDGET_ENABLED"),
        "FEEDBACK_DISCORD_WEBHOOK_URL": _app.config.get("FEEDBACK_DISCORD_WEBHOOK_URL"),
        "FEEDBACK_SLACK_WEBHOOK_URL": _app.config.get("FEEDBACK_SLACK_WEBHOOK_URL"),
        "FEEDBACK_RATE_LIMIT_PER_HOUR": _app.config.get("FEEDBACK_RATE_LIMIT_PER_HOUR"),
    }
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REQUIRE_MFA"] = False
    _app.config["FEEDBACK_WIDGET_ENABLED"] = True
    _app.config["FEEDBACK_DISCORD_WEBHOOK_URL"] = None
    _app.config["FEEDBACK_SLACK_WEBHOOK_URL"] = None
    _app.config["FEEDBACK_RATE_LIMIT_PER_HOUR"] = 10

    with _app.app_context():
        yield _app

    _app.config.update(original_config)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def dispatch_calls(monkeypatch):
    """Capture notification dispatches instead of enqueueing to the broker."""
    calls = []
    monkeypatch.setattr(
        notify_tasks.dispatch_feedback_notification,
        "delay",
        lambda feedback_id: calls.append(feedback_id),
    )
    return calls


class _FakeResponse:
    status_code = 204
    text = ""


@pytest.fixture
def webhook_posts(monkeypatch):
    calls = []

    def _fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse()

    monkeypatch.setattr("cabotage.celery.tasks.notify.requests.post", _fake_post)
    return calls


@pytest.fixture
def regular_user(app):
    user = User(
        username=f"feedback-user-{uuid.uuid4().hex[:8]}",
        email=f"feedback-user-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db.session.add(user)
    db.session.commit()
    yield user
    db.session.query(Feedback).filter_by(user_id=user.id).delete()
    db.session.query(User).filter_by(id=user.id).delete()
    db.session.commit()


def _login(client, user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"


def _cleanup(feedback_id):
    db.session.query(Feedback).filter_by(id=feedback_id).delete()
    db.session.commit()


def _make_feedback(**kwargs):
    kwargs.setdefault("kind", "other")
    kwargs.setdefault("message", "test message")
    feedback = Feedback(**kwargs)
    db.session.add(feedback)
    db.session.commit()
    return feedback


def test_widget_rendered_when_enabled(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"feedback-widget" in response.data


def test_widget_hidden_when_disabled(app, client):
    app.config["FEEDBACK_WIDGET_ENABLED"] = False
    response = client.get("/")
    assert response.status_code == 200
    assert b"feedback-widget" not in response.data


def test_submit_returns_404_when_disabled(app, client):
    app.config["FEEDBACK_WIDGET_ENABLED"] = False
    response = client.post("/feedback", json={"message": "hello"})
    assert response.status_code == 404


def test_submit_requires_message(client):
    response = client.post("/feedback", json={"message": "   "})
    assert response.status_code == 400


def test_submit_rejects_overlong_message(client):
    response = client.post("/feedback", json={"message": "x" * 5001})
    assert response.status_code == 400


def test_anonymous_submit_stores_feedback(client, dispatch_calls):
    response = client.post(
        "/feedback",
        json={
            "message": "the deploy page is great",
            "kind": "idea",
            "email": "visitor@example.com",
            "page_url": "https://paas.example.com/projects/acme/site?tok=x#frag",
            "page_title": "acme/site - Cabotage",
            "endpoint": "user.project",
            "view_args": {"org_slug": "acme", "project_slug": "site"},
            "viewport": "1440x900",
            "theme": "dark",
        },
    )
    assert response.status_code == 201
    feedback = (
        db.session.query(Feedback).filter_by(email="visitor@example.com").one_or_none()
    )
    assert feedback is not None
    assert feedback.user_id is None
    assert feedback.kind == "idea"
    assert feedback.message == "the deploy page is great"
    assert feedback.page_url == "https://paas.example.com/projects/acme/site"
    assert feedback.endpoint == "user.project"
    assert feedback.view_args == {"org_slug": "acme", "project_slug": "site"}
    assert feedback.viewport == "1440x900"
    assert feedback.theme == "dark"
    assert dispatch_calls == [str(feedback.id)]
    _cleanup(feedback.id)


def test_submit_defaults_invalid_kind_to_other(client):
    response = client.post(
        "/feedback",
        json={"message": "kind check", "kind": "exploit", "email": "kind@example.com"},
    )
    assert response.status_code == 201
    feedback = db.session.query(Feedback).filter_by(email="kind@example.com").one()
    assert feedback.kind == "other"
    _cleanup(feedback.id)


def test_submit_ignores_non_dict_view_args(client):
    response = client.post(
        "/feedback",
        json={
            "message": "view args check",
            "email": "viewargs@example.com",
            "view_args": ["not", "a", "dict"],
        },
    )
    assert response.status_code == 201
    feedback = db.session.query(Feedback).filter_by(email="viewargs@example.com").one()
    assert feedback.view_args == {}
    _cleanup(feedback.id)


def test_authenticated_submit_records_user(client, regular_user):
    _login(client, regular_user)
    response = client.post("/feedback", json={"message": "logged-in feedback"})
    assert response.status_code == 201
    feedback = db.session.query(Feedback).filter_by(user_id=regular_user.id).one()
    assert feedback.message == "logged-in feedback"


def test_submit_survives_enqueue_failure(client, monkeypatch):
    def _boom(feedback_id):
        raise RuntimeError("broker down")

    monkeypatch.setattr(notify_tasks.dispatch_feedback_notification, "delay", _boom)
    response = client.post(
        "/feedback", json={"message": "still stored", "email": "boom@example.com"}
    )
    assert response.status_code == 201
    feedback = db.session.query(Feedback).filter_by(email="boom@example.com").one()
    _cleanup(feedback.id)


def test_rate_limit(app, client):
    rows = [
        Feedback(kind="other", message=f"filler {i}", remote_addr="127.0.0.1")
        for i in range(10)
    ]
    db.session.add_all(rows)
    db.session.commit()
    try:
        response = client.post(
            "/feedback", json={"message": "over the limit", "email": "rl@example.com"}
        )
        assert response.status_code == 429
        assert (
            db.session.query(Feedback).filter_by(email="rl@example.com").one_or_none()
            is None
        )
    finally:
        for row in rows:
            _cleanup(row.id)


def test_csrf_required_when_enabled(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    response = client.post(
        "/feedback", json={"message": "no token", "email": "csrf@example.com"}
    )
    assert response.status_code == 400
    assert (
        db.session.query(Feedback).filter_by(email="csrf@example.com").one_or_none()
        is None
    )


def test_dispatch_posts_to_discord(app, webhook_posts):
    app.config["FEEDBACK_DISCORD_WEBHOOK_URL"] = "https://discord.test/webhook"
    feedback = _make_feedback(
        kind="bug",
        message="webhook check",
        email="webhook@example.com",
        page_url="https://paas.example.com/projects",
    )
    notify_tasks.dispatch_feedback_notification(str(feedback.id))
    assert len(webhook_posts) == 1
    url, kwargs = webhook_posts[0]
    assert url == "https://discord.test/webhook"
    embed = kwargs["json"]["embeds"][0]
    assert embed["title"] == "New feedback: bug"
    assert "webhook check" in embed["description"]
    assert "From: webhook@example.com" in embed["description"]
    assert "https://paas.example.com/projects" in embed["description"]
    assert embed["color"] == notify_tasks.DISCORD_RED
    _cleanup(feedback.id)


def test_dispatch_posts_to_slack(app, webhook_posts):
    app.config["FEEDBACK_SLACK_WEBHOOK_URL"] = "https://hooks.slack.test/services/x"
    feedback = _make_feedback(kind="idea", message="slack check")
    notify_tasks.dispatch_feedback_notification(str(feedback.id))
    assert len(webhook_posts) == 1
    url, kwargs = webhook_posts[0]
    assert url == "https://hooks.slack.test/services/x"
    attachment = kwargs["json"]["attachments"][0]
    assert attachment["color"] == notify_tasks.COLOR_GREEN
    assert "slack check" in attachment["blocks"][0]["text"]["text"]
    assert "From: anonymous" in attachment["blocks"][0]["text"]["text"]
    _cleanup(feedback.id)


def test_dispatch_posts_to_both_when_configured(app, webhook_posts):
    app.config["FEEDBACK_DISCORD_WEBHOOK_URL"] = "https://discord.test/webhook"
    app.config["FEEDBACK_SLACK_WEBHOOK_URL"] = "https://hooks.slack.test/services/x"
    feedback = _make_feedback(message="both")
    notify_tasks.dispatch_feedback_notification(str(feedback.id))
    assert [url for url, _ in webhook_posts] == [
        "https://discord.test/webhook",
        "https://hooks.slack.test/services/x",
    ]
    _cleanup(feedback.id)


def test_dispatch_skipped_when_unconfigured(app, webhook_posts):
    feedback = _make_feedback(message="no webhook")
    notify_tasks.dispatch_feedback_notification(str(feedback.id))
    assert webhook_posts == []
    _cleanup(feedback.id)


def test_dispatch_survives_webhook_failure(app, monkeypatch):
    import requests as requests_lib

    def _boom(url, **kwargs):
        raise requests_lib.ConnectionError("discord down")

    monkeypatch.setattr("cabotage.celery.tasks.notify.requests.post", _boom)
    app.config["FEEDBACK_DISCORD_WEBHOOK_URL"] = "https://discord.test/webhook"
    feedback = _make_feedback(message="still fine")
    notify_tasks.dispatch_feedback_notification(str(feedback.id))
    _cleanup(feedback.id)
