"""Tests for Slack and Discord OAuth integration flows."""

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import (
    DiscordIntegration,
    Organization,
    SlackIntegration,
    User,
)
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.wsgi import app as _app


SLACK_CLIENT_ID = "10818900810177.1079000000000"
SLACK_CLIENT_SECRET = "slack-test-secret"
DISCORD_CLIENT_ID = "1234567890"
DISCORD_CLIENT_SECRET = "discord-test-secret"
DISCORD_BOT_TOKEN = "MTIz.abc.def"


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REQUIRE_MFA"] = False
    _app.config["SLACK_CLIENT_ID"] = SLACK_CLIENT_ID
    _app.config["SLACK_CLIENT_SECRET"] = SLACK_CLIENT_SECRET
    _app.config["DISCORD_CLIENT_ID"] = DISCORD_CLIENT_ID
    _app.config["DISCORD_CLIENT_SECRET"] = DISCORD_CLIENT_SECRET
    _app.config["DISCORD_BOT_TOKEN"] = DISCORD_BOT_TOKEN

    # Blueprints are only registered when config is set at init time.
    # Since the test app is already created, register them now if missing.
    from cabotage.server.integrations.slack_oauth import slack_oauth_bp
    from cabotage.server.integrations.discord_oauth import discord_oauth_bp

    # Reset first-request flag so blueprints can be registered after other
    # test modules have already made requests on the shared app object.
    _app._got_first_request = False
    if "slack_oauth" not in _app.blueprints:
        _app.register_blueprint(slack_oauth_bp)
    if "discord_oauth" not in _app.blueprints:
        _app.register_blueprint(discord_oauth_bp)

    with _app.app_context():
        yield _app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_user(app):
    u = User(
        username=f"testadmin-{uuid.uuid4().hex[:8]}",
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
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
def non_admin_user(app):
    u = User(
        username=f"testuser-{uuid.uuid4().hex[:8]}",
        email=f"user-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
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
def org(admin_user):
    o = Organization(name="Test Org", slug=f"testorg-{uuid.uuid4().hex[:8]}")
    db.session.add(o)
    db.session.flush()
    membership = OrganizationMember(
        organization_id=o.id,
        user_id=admin_user.id,
        admin=True,
    )
    db.session.add(membership)
    db.session.commit()
    yield o
    # Clean up
    OrganizationMember.query.filter_by(organization_id=o.id).delete()
    SlackIntegration.query.filter_by(organization_id=o.id).delete()
    DiscordIntegration.query.filter_by(organization_id=o.id).delete()
    db.session.delete(o)
    db.session.commit()


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        # Flask-Principal stores the identity in the session
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"


# --- Slack Tests ---


class TestSlackConnect:
    def test_connect_redirects_to_slack(self, client, admin_user, org):
        _login(client, admin_user)
        resp = client.get(f"/integrations/slack/connect/{org.slug}")
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "slack.com/oauth/v2/authorize" in location
        assert f"client_id={SLACK_CLIENT_ID}" in location
        assert "chat:write" in location

    def test_connect_requires_login(self, client, org):
        resp = client.get(f"/integrations/slack/connect/{org.slug}")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"] or resp.status_code == 401

    def test_connect_requires_admin(self, client, non_admin_user, org):
        _login(client, non_admin_user)
        resp = client.get(f"/integrations/slack/connect/{org.slug}")
        assert resp.status_code == 403

    def test_connect_404_for_bad_slug(self, client, admin_user):
        _login(client, admin_user)
        resp = client.get("/integrations/slack/connect/nonexistent-org")
        assert resp.status_code == 404


class TestSlackCallback:
    def test_callback_invalid_state_rejected(self, client, admin_user, org):
        _login(client, admin_user)
        with client.session_transaction() as sess:
            sess["slack_oauth_state"] = "expected-state"
            sess["slack_oauth_org_slug"] = org.slug

        resp = client.get(
            "/integrations/slack/callback?state=wrong-state&code=test-code"
        )
        assert resp.status_code == 302

    def test_callback_missing_state_rejected(self, client, admin_user, org):
        _login(client, admin_user)
        resp = client.get("/integrations/slack/callback?code=test-code")
        assert resp.status_code == 302

    @patch("cabotage.server.integrations.slack_oauth.vault")
    @patch("cabotage.server.integrations.slack_oauth.http_requests")
    def test_callback_success_creates_integration(
        self, mock_requests, mock_vault, client, admin_user, org
    ):
        _login(client, admin_user)

        mock_vault.vault_prefix = "cabotage-secrets"
        mock_vault.vault_connection = MagicMock()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "access_token": "xoxb-test-token",
            "team": {"id": "T12345", "name": "Test Workspace"},
            "bot_user_id": "U12345",
        }
        mock_requests.post.return_value = mock_response
        mock_requests.RequestException = Exception

        state = "test-state-value"
        with client.session_transaction() as sess:
            sess["slack_oauth_state"] = state
            sess["slack_oauth_org_slug"] = org.slug

        resp = client.get(
            f"/integrations/slack/callback?state={state}&code=test-auth-code"
        )
        assert resp.status_code == 302

        integration = SlackIntegration.query.filter_by(organization_id=org.id).first()
        assert integration is not None
        assert integration.team_id == "T12345"
        assert integration.team_name == "Test Workspace"
        assert integration.bot_user_id == "U12345"
        assert integration.installed_by_user_id == admin_user.id

        mock_vault.vault_connection.write.assert_called_once()

    @patch("cabotage.server.integrations.slack_oauth.http_requests")
    def test_callback_slack_error_flashes_message(
        self, mock_requests, client, admin_user, org
    ):
        _login(client, admin_user)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": False,
            "error": "invalid_code",
        }
        mock_requests.post.return_value = mock_response
        mock_requests.RequestException = Exception

        state = "test-state"
        with client.session_transaction() as sess:
            sess["slack_oauth_state"] = state
            sess["slack_oauth_org_slug"] = org.slug

        resp = client.get(f"/integrations/slack/callback?state={state}&code=bad-code")
        assert resp.status_code == 302
        assert SlackIntegration.query.filter_by(organization_id=org.id).first() is None


class TestSlackDisconnect:
    @patch("cabotage.server.integrations.slack_oauth._slack_leave_channel")
    @patch("cabotage.server.integrations.slack_oauth._send_slack_message")
    @patch("cabotage.server.integrations.slack_oauth.vault")
    def test_disconnect_removes_integration(
        self, mock_vault, mock_send, mock_leave, client, admin_user, org
    ):
        mock_vault.vault_connection = MagicMock()

        integration = SlackIntegration(
            organization_id=org.id,
            team_id="T12345",
            team_name="Test Workspace",
            access_token_vault_path="cabotage-secrets/integrations/slack/test",
            default_channel_id="C001",
            default_channel_name="alerts",
            installed_by_user_id=admin_user.id,
        )
        db.session.add(integration)
        db.session.commit()

        _login(client, admin_user)
        resp = client.post(f"/integrations/slack/disconnect/{org.slug}")
        assert resp.status_code == 302
        assert SlackIntegration.query.filter_by(organization_id=org.id).first() is None
        mock_vault.vault_connection.delete.assert_called_once()
        mock_send.assert_called_once()
        assert "disconnected" in mock_send.call_args[0][2]
        mock_leave.assert_called_once()

    def test_disconnect_requires_admin(self, client, non_admin_user, org):
        _login(client, non_admin_user)
        resp = client.post(f"/integrations/slack/disconnect/{org.slug}")
        assert resp.status_code == 403


class TestSlackUpdateChannel:
    @patch("cabotage.server.integrations.slack_oauth._slack_leave_channel")
    @patch("cabotage.server.integrations.slack_oauth._slack_join_channel")
    @patch("cabotage.server.integrations.slack_oauth._send_slack_message")
    def test_update_channel(
        self, mock_send, mock_join, mock_leave, client, admin_user, org
    ):
        integration = SlackIntegration(
            organization_id=org.id,
            team_id="T12345",
            team_name="Test Workspace",
            installed_by_user_id=admin_user.id,
        )
        db.session.add(integration)
        db.session.commit()

        _login(client, admin_user)
        resp = client.post(
            f"/integrations/slack/channel/{org.slug}",
            data={"channel_id": "C99999", "channel_name": "alerts"},
        )
        assert resp.status_code == 302

        db.session.refresh(integration)
        assert integration.default_channel_id == "C99999"
        assert integration.default_channel_name == "alerts"
        mock_send.assert_called_once()
        assert "default notification channel" in mock_send.call_args[0][2]
        mock_join.assert_called_once()
        mock_leave.assert_not_called()

    @patch("cabotage.server.integrations.slack_oauth._slack_leave_channel")
    @patch("cabotage.server.integrations.slack_oauth._slack_join_channel")
    @patch("cabotage.server.integrations.slack_oauth._send_slack_message")
    def test_update_channel_sends_move_message(
        self, mock_send, mock_join, mock_leave, client, admin_user, org
    ):
        integration = SlackIntegration(
            organization_id=org.id,
            team_id="T12345",
            team_name="Test Workspace",
            default_channel_id="C00001",
            default_channel_name="old-channel",
            installed_by_user_id=admin_user.id,
        )
        db.session.add(integration)
        db.session.commit()

        _login(client, admin_user)
        resp = client.post(
            f"/integrations/slack/channel/{org.slug}",
            data={"channel_id": "C99999", "channel_name": "alerts"},
        )
        assert resp.status_code == 302
        # Two messages: one to old channel (moved), one to new channel (delivered)
        assert mock_send.call_count == 2
        old_msg = mock_send.call_args_list[0][0][2]
        new_msg = mock_send.call_args_list[1][0][2]
        assert "moved" in old_msg
        assert "default notification channel" in new_msg
        mock_join.assert_called_once()
        mock_leave.assert_called_once()

    def test_update_channel_no_integration(self, client, admin_user, org):
        _login(client, admin_user)
        resp = client.post(
            f"/integrations/slack/channel/{org.slug}",
            data={"channel_id": "C99999", "channel_name": "alerts"},
        )
        assert resp.status_code == 302


class TestSlackListChannels:
    @patch("cabotage.server.integrations.slack_oauth.get_slack_channels")
    def test_list_channels_returns_json(
        self, mock_get_channels, client, admin_user, org
    ):
        integration = SlackIntegration(
            organization_id=org.id,
            team_id="T12345",
            team_name="Test Workspace",
            installed_by_user_id=admin_user.id,
        )
        db.session.add(integration)
        db.session.commit()

        mock_get_channels.return_value = [
            {"id": "C001", "name": "general"},
            {"id": "C002", "name": "alerts"},
        ]

        _login(client, admin_user)
        resp = client.get(f"/integrations/slack/channels/{org.slug}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["channels"]) == 2
        assert data["channels"][0]["name"] == "general"
        assert data["channels"][1]["id"] == "C002"

    @patch("cabotage.server.integrations.slack_oauth.get_slack_channels")
    def test_list_channels_empty_when_no_integration(
        self, mock_get_channels, client, admin_user, org
    ):
        mock_get_channels.return_value = []
        _login(client, admin_user)
        resp = client.get(f"/integrations/slack/channels/{org.slug}")
        assert resp.status_code == 200
        assert resp.get_json()["channels"] == []

    def test_list_channels_requires_admin(self, client, non_admin_user, org):
        _login(client, non_admin_user)
        resp = client.get(f"/integrations/slack/channels/{org.slug}")
        assert resp.status_code == 403

    def test_list_channels_requires_login(self, client, org):
        resp = client.get(f"/integrations/slack/channels/{org.slug}")
        assert resp.status_code == 302


# --- Discord Tests ---


class TestDiscordConnect:
    def test_connect_redirects_to_discord(self, client, admin_user, org):
        _login(client, admin_user)
        resp = client.get(f"/integrations/discord/connect/{org.slug}")
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "discord.com/api/oauth2/authorize" in location
        assert f"client_id={DISCORD_CLIENT_ID}" in location
        assert "permissions=2048" in location

    def test_connect_requires_login(self, client, org):
        resp = client.get(f"/integrations/discord/connect/{org.slug}")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"] or resp.status_code == 401

    def test_connect_requires_admin(self, client, non_admin_user, org):
        _login(client, non_admin_user)
        resp = client.get(f"/integrations/discord/connect/{org.slug}")
        assert resp.status_code == 403


class TestDiscordCallback:
    def test_callback_invalid_state_rejected(self, client, admin_user, org):
        _login(client, admin_user)
        with client.session_transaction() as sess:
            sess["discord_oauth_state"] = "expected-state"
            sess["discord_oauth_org_slug"] = org.slug

        resp = client.get(
            "/integrations/discord/callback?state=wrong-state&code=test-code"
        )
        assert resp.status_code == 302

    @patch("cabotage.server.integrations.discord_oauth.http_requests")
    def test_callback_success_creates_integration(
        self, mock_requests, client, admin_user, org
    ):
        _login(client, admin_user)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "discord-access-token",
            "guild": {"id": "G12345", "name": "Test Server"},
        }
        mock_requests.post.return_value = mock_response
        mock_requests.RequestException = Exception

        state = "test-state-value"
        with client.session_transaction() as sess:
            sess["discord_oauth_state"] = state
            sess["discord_oauth_org_slug"] = org.slug

        resp = client.get(
            f"/integrations/discord/callback?state={state}&code=test-auth-code"
        )
        assert resp.status_code == 302

        integration = DiscordIntegration.query.filter_by(organization_id=org.id).first()
        assert integration is not None
        assert integration.guild_id == "G12345"
        assert integration.guild_name == "Test Server"
        assert integration.installed_by_user_id == admin_user.id

    @patch("cabotage.server.integrations.discord_oauth.http_requests")
    def test_callback_discord_error_flashes_message(
        self, mock_requests, client, admin_user, org
    ):
        _login(client, admin_user)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "error": "invalid_grant",
            "error_description": "Invalid code",
        }
        mock_requests.post.return_value = mock_response
        mock_requests.RequestException = Exception

        state = "test-state"
        with client.session_transaction() as sess:
            sess["discord_oauth_state"] = state
            sess["discord_oauth_org_slug"] = org.slug

        resp = client.get(f"/integrations/discord/callback?state={state}&code=bad-code")
        assert resp.status_code == 302
        assert (
            DiscordIntegration.query.filter_by(organization_id=org.id).first() is None
        )

    @patch("cabotage.server.integrations.discord_oauth._fetch_guild_name")
    @patch("cabotage.server.integrations.discord_oauth.http_requests")
    def test_callback_with_guild_id_param(
        self, mock_requests, mock_fetch, client, admin_user, org
    ):
        """Discord sends guild_id as a query param when adding a bot."""
        _login(client, admin_user)
        mock_fetch.return_value = "Fetched Server"

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "discord-access-token",
            "guild": {},
        }
        mock_requests.post.return_value = mock_response
        mock_requests.RequestException = Exception

        state = "test-state"
        with client.session_transaction() as sess:
            sess["discord_oauth_state"] = state
            sess["discord_oauth_org_slug"] = org.slug

        resp = client.get(
            f"/integrations/discord/callback?state={state}&code=test-code&guild_id=G99999"
        )
        assert resp.status_code == 302

        integration = DiscordIntegration.query.filter_by(organization_id=org.id).first()
        assert integration is not None
        assert integration.guild_id == "G99999"
        assert integration.guild_name == "Fetched Server"


class TestDiscordDisconnect:
    @patch("cabotage.server.integrations.discord_oauth._send_discord_message")
    def test_disconnect_removes_integration(self, mock_send, client, admin_user, org):
        integration = DiscordIntegration(
            organization_id=org.id,
            guild_id="G12345",
            guild_name="Test Server",
            default_channel_id="C001",
            default_channel_name="alerts",
            installed_by_user_id=admin_user.id,
        )
        db.session.add(integration)
        db.session.commit()

        _login(client, admin_user)
        resp = client.post(f"/integrations/discord/disconnect/{org.slug}")
        assert resp.status_code == 302
        assert (
            DiscordIntegration.query.filter_by(organization_id=org.id).first() is None
        )
        mock_send.assert_called_once()
        assert "disconnected" in mock_send.call_args[0][1]

    def test_disconnect_requires_admin(self, client, non_admin_user, org):
        _login(client, non_admin_user)
        resp = client.post(f"/integrations/discord/disconnect/{org.slug}")
        assert resp.status_code == 403


class TestDiscordUpdateChannel:
    @patch("cabotage.server.integrations.discord_oauth._send_discord_message")
    def test_update_channel(self, mock_send, client, admin_user, org):
        integration = DiscordIntegration(
            organization_id=org.id,
            guild_id="G12345",
            guild_name="Test Server",
            installed_by_user_id=admin_user.id,
        )
        db.session.add(integration)
        db.session.commit()

        _login(client, admin_user)
        resp = client.post(
            f"/integrations/discord/channel/{org.slug}",
            data={"channel_id": "C99999", "channel_name": "alerts"},
        )
        assert resp.status_code == 302

        db.session.refresh(integration)
        assert integration.default_channel_id == "C99999"
        assert integration.default_channel_name == "alerts"
        mock_send.assert_called_once()
        assert "default notification channel" in mock_send.call_args[0][1]

    @patch("cabotage.server.integrations.discord_oauth._send_discord_message")
    def test_update_channel_sends_move_message(
        self, mock_send, client, admin_user, org
    ):
        integration = DiscordIntegration(
            organization_id=org.id,
            guild_id="G12345",
            guild_name="Test Server",
            default_channel_id="C00001",
            default_channel_name="old-channel",
            installed_by_user_id=admin_user.id,
        )
        db.session.add(integration)
        db.session.commit()

        _login(client, admin_user)
        resp = client.post(
            f"/integrations/discord/channel/{org.slug}",
            data={"channel_id": "C99999", "channel_name": "alerts"},
        )
        assert resp.status_code == 302
        assert mock_send.call_count == 2
        old_msg = mock_send.call_args_list[0][0][1]
        new_msg = mock_send.call_args_list[1][0][1]
        assert "moved" in old_msg
        assert "default notification channel" in new_msg


class TestDiscordListChannels:
    @patch("cabotage.server.integrations.discord_oauth.get_discord_channels")
    def test_list_channels_returns_json(
        self, mock_get_channels, client, admin_user, org
    ):
        integration = DiscordIntegration(
            organization_id=org.id,
            guild_id="G12345",
            guild_name="Test Server",
            installed_by_user_id=admin_user.id,
        )
        db.session.add(integration)
        db.session.commit()

        mock_get_channels.return_value = [
            {"id": "C001", "name": "general"},
            {"id": "C002", "name": "alerts"},
        ]

        _login(client, admin_user)
        resp = client.get(f"/integrations/discord/channels/{org.slug}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["channels"]) == 2
        assert data["channels"][0]["name"] == "general"
        assert data["channels"][1]["id"] == "C002"

    @patch("cabotage.server.integrations.discord_oauth.get_discord_channels")
    def test_list_channels_empty_when_no_integration(
        self, mock_get_channels, client, admin_user, org
    ):
        mock_get_channels.return_value = []
        _login(client, admin_user)
        resp = client.get(f"/integrations/discord/channels/{org.slug}")
        assert resp.status_code == 200
        assert resp.get_json()["channels"] == []

    def test_list_channels_requires_admin(self, client, non_admin_user, org):
        _login(client, non_admin_user)
        resp = client.get(f"/integrations/discord/channels/{org.slug}")
        assert resp.status_code == 403

    def test_list_channels_requires_login(self, client, org):
        resp = client.get(f"/integrations/discord/channels/{org.slug}")
        assert resp.status_code == 302
