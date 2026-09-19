import time
import uuid

import pytest
from flask_security import hash_password

from cabotage.celery.tasks import feedback as feedback_tasks
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
    }
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REQUIRE_MFA"] = False
    _app.config["FEEDBACK_WIDGET_ENABLED"] = True
    _app.config["FEEDBACK_DISCORD_WEBHOOK_URL"] = None
    _app.config["FEEDBACK_SLACK_WEBHOOK_URL"] = None

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
        feedback_tasks.dispatch_feedback_notification,
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

    monkeypatch.setattr("cabotage.celery.tasks.feedback.requests.post", _fake_post)
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


@pytest.fixture
def auth_client(client, regular_user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = regular_user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = regular_user.id
        sess["identity.auth_type"] = "session"
    return client


def _cleanup(feedback_id):
    db.session.query(Feedback).filter_by(id=feedback_id).delete()
    db.session.commit()


def _only_feedback(user):
    return db.session.query(Feedback).filter_by(user_id=user.id).one()


def test_widget_rendered_for_authenticated_user(auth_client):
    response = auth_client.get("/")
    assert response.status_code == 200
    assert b"feedback-widget" in response.data


def test_widget_hidden_from_anonymous_visitors(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"feedback-widget" not in response.data


def test_widget_hidden_when_disabled(app, auth_client):
    app.config["FEEDBACK_WIDGET_ENABLED"] = False
    response = auth_client.get("/")
    assert response.status_code == 200
    assert b"feedback-widget" not in response.data


def test_submit_requires_authentication(client):
    response = client.post("/feedback", json={"message": "anonymous attempt"})
    assert response.status_code in (301, 302, 401)
    assert (
        db.session.query(Feedback).filter_by(message="anonymous attempt").count() == 0
    )


def test_submit_returns_404_when_disabled(app, auth_client):
    app.config["FEEDBACK_WIDGET_ENABLED"] = False
    response = auth_client.post("/feedback", json={"message": "hello"})
    assert response.status_code == 404


def test_submit_requires_message(auth_client):
    assert auth_client.post("/feedback", json={"message": "   "}).status_code == 400


def test_submit_rejects_overlong_message(auth_client):
    response = auth_client.post("/feedback", json={"message": "x" * 5001})
    assert response.status_code == 400


@pytest.mark.parametrize("payload", [[], ["a"], "text", 5, {"message": 1}])
def test_submit_rejects_malformed_payloads(auth_client, payload):
    """Unexpected JSON shapes are a 400, never a 500."""
    response = auth_client.post("/feedback", json=payload)
    assert response.status_code == 400


def test_submit_ignores_non_string_fields(auth_client, regular_user):
    response = auth_client.post(
        "/feedback",
        json={"message": "typed fields", "page_title": 42, "theme": ["dark"]},
    )
    assert response.status_code == 201
    feedback = _only_feedback(regular_user)
    assert feedback.page_title is None
    assert feedback.theme is None


def test_submit_stores_feedback(auth_client, regular_user, dispatch_calls):
    response = auth_client.post(
        "/feedback",
        json={
            "message": "the deploy page is great",
            "kind": "idea",
            "page_url": "https://paas.example.com/projects/acme/site?tok=x#frag",
            "page_title": "acme/site - Cabotage",
            "endpoint": "user.project",
            "view_args": {"org_slug": "acme", "project_slug": "site"},
            "viewport": "1440x900",
            "theme": "dark",
        },
    )
    assert response.status_code == 201
    feedback = _only_feedback(regular_user)
    assert feedback.user_id == regular_user.id
    assert feedback.kind == "idea"
    assert feedback.message == "the deploy page is great"
    assert feedback.page_url == "https://paas.example.com/projects/acme/site"
    assert feedback.endpoint == "user.project"
    assert feedback.view_args == {"org_slug": "acme", "project_slug": "site"}
    assert feedback.viewport == "1440x900"
    assert feedback.theme == "dark"
    assert dispatch_calls == [str(feedback.id)]


def test_submit_defaults_invalid_kind_to_other(auth_client, regular_user):
    response = auth_client.post(
        "/feedback", json={"message": "kind check", "kind": "exploit"}
    )
    assert response.status_code == 201
    assert _only_feedback(regular_user).kind == "other"


def test_submit_ignores_non_dict_view_args(auth_client, regular_user):
    response = auth_client.post(
        "/feedback", json={"message": "view args", "view_args": ["not", "a", "dict"]}
    )
    assert response.status_code == 201
    assert _only_feedback(regular_user).view_args == {}


def test_submit_survives_enqueue_failure(auth_client, regular_user, monkeypatch):
    def _boom(feedback_id):
        raise RuntimeError("broker down")

    monkeypatch.setattr(feedback_tasks.dispatch_feedback_notification, "delay", _boom)
    response = auth_client.post("/feedback", json={"message": "still stored"})
    assert response.status_code == 201
    assert _only_feedback(regular_user).message == "still stored"


def test_csrf_required_when_enabled(app, auth_client, regular_user):
    app.config["WTF_CSRF_ENABLED"] = True
    response = auth_client.post("/feedback", json={"message": "no token"})
    assert response.status_code == 400
    assert db.session.query(Feedback).filter_by(user_id=regular_user.id).count() == 0


def _make_feedback(user, **kwargs):
    kwargs.setdefault("kind", "other")
    kwargs.setdefault("message", "test message")
    feedback = Feedback(user_id=user.id, **kwargs)
    db.session.add(feedback)
    db.session.commit()
    return feedback


def test_dispatch_posts_to_discord(app, regular_user, webhook_posts):
    app.config["FEEDBACK_DISCORD_WEBHOOK_URL"] = "https://discord.test/webhook"
    feedback = _make_feedback(
        regular_user,
        kind="bug",
        message="webhook check",
        page_url="https://paas.example.com/projects",
    )
    feedback_tasks.dispatch_feedback_notification(str(feedback.id))
    assert len(webhook_posts) == 1
    url, kwargs = webhook_posts[0]
    assert url == "https://discord.test/webhook"
    embed = kwargs["json"]["embeds"][0]
    assert embed["title"] == "New feedback: bug"
    assert "webhook check" in embed["description"]
    assert f"From: {regular_user.username}" in embed["description"]
    assert "https://paas.example.com/projects" in embed["description"]
    assert embed["color"] == notify_tasks.DISCORD_RED
    _cleanup(feedback.id)


def test_dispatch_posts_to_slack(app, regular_user, webhook_posts):
    app.config["FEEDBACK_SLACK_WEBHOOK_URL"] = "https://hooks.slack.test/services/x"
    feedback = _make_feedback(regular_user, kind="idea", message="slack check")
    feedback_tasks.dispatch_feedback_notification(str(feedback.id))
    assert len(webhook_posts) == 1
    url, kwargs = webhook_posts[0]
    assert url == "https://hooks.slack.test/services/x"
    attachment = kwargs["json"]["attachments"][0]
    assert attachment["color"] == notify_tasks.COLOR_GREEN
    assert "slack check" in attachment["blocks"][0]["text"]["text"]
    _cleanup(feedback.id)


def test_dispatch_posts_to_both_when_configured(app, regular_user, webhook_posts):
    app.config["FEEDBACK_DISCORD_WEBHOOK_URL"] = "https://discord.test/webhook"
    app.config["FEEDBACK_SLACK_WEBHOOK_URL"] = "https://hooks.slack.test/services/x"
    feedback = _make_feedback(regular_user, message="both")
    feedback_tasks.dispatch_feedback_notification(str(feedback.id))
    assert [url for url, _ in webhook_posts] == [
        "https://discord.test/webhook",
        "https://hooks.slack.test/services/x",
    ]
    _cleanup(feedback.id)


def test_dispatch_fits_platform_payload_limits(app, regular_user, webhook_posts):
    """Long submissions must not exceed Slack/Discord body limits."""
    app.config["FEEDBACK_DISCORD_WEBHOOK_URL"] = "https://discord.test/webhook"
    app.config["FEEDBACK_SLACK_WEBHOOK_URL"] = "https://hooks.slack.test/services/x"
    feedback = _make_feedback(
        regular_user,
        message="x" * 5000,
        page_url="https://paas.example.com/" + "p" * 2000,
        endpoint="user.project",
        view_args={"org_slug": "a" * 60, "project_slug": "b" * 60},
        viewport="1440x900",
        theme="dark",
    )
    feedback_tasks.dispatch_feedback_notification(str(feedback.id))
    discord_body = webhook_posts[0][1]["json"]["embeds"][0]["description"]
    slack_body = webhook_posts[1][1]["json"]["attachments"][0]["blocks"][0]["text"][
        "text"
    ]
    assert len(discord_body) <= 4096
    assert len(slack_body) <= 3000
    assert feedback.page_url in discord_body
    _cleanup(feedback.id)


def test_dispatch_skipped_when_unconfigured(app, regular_user, webhook_posts):
    feedback = _make_feedback(regular_user, message="no webhook")
    feedback_tasks.dispatch_feedback_notification(str(feedback.id))
    assert webhook_posts == []
    _cleanup(feedback.id)


def test_dispatch_survives_webhook_failure(app, regular_user, monkeypatch):
    import requests as requests_lib

    def _boom(url, **kwargs):
        raise requests_lib.ConnectionError("discord down")

    monkeypatch.setattr("cabotage.celery.tasks.feedback.requests.post", _boom)
    app.config["FEEDBACK_DISCORD_WEBHOOK_URL"] = "https://discord.test/webhook"
    feedback = _make_feedback(regular_user, message="still fine")
    feedback_tasks.dispatch_feedback_notification(str(feedback.id))
    _cleanup(feedback.id)
