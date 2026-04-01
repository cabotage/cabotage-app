import logging
import secrets

import requests as http_requests
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    session,
    url_for,
)
from flask_security import current_user, login_required

from cabotage.server import db, vault
from cabotage.server.acl import AdministerOrganizationPermission
from cabotage.server.models.auth import Organization, SlackIntegration

log = logging.getLogger(__name__)

slack_oauth_bp = Blueprint("slack_oauth", __name__, url_prefix="/integrations/slack")

SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"


@slack_oauth_bp.route("/connect/<org_slug>")
@login_required
def connect(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)

    client_id = current_app.config.get("SLACK_CLIENT_ID")
    if not client_id:
        flash("Slack integration is not configured.", "error")
        return redirect(url_for("user.organization_settings", org_slug=org_slug))

    state = secrets.token_urlsafe(32)
    session["slack_oauth_state"] = state
    session["slack_oauth_org_slug"] = org_slug

    scheme = current_app.config["EXT_PREFERRED_URL_SCHEME"]
    server = current_app.config["EXT_SERVER_NAME"]
    callback_path = url_for("slack_oauth.callback")
    redirect_uri = f"{scheme}://{server}{callback_path}"

    scopes = "chat:write,channels:join,channels:read,groups:read"

    authorize_url = (
        f"{SLACK_AUTHORIZE_URL}"
        f"?client_id={client_id}"
        f"&scope={scopes}"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
    )
    return redirect(authorize_url)


@slack_oauth_bp.route("/callback")
@login_required
def callback():
    error = request_arg("error")
    if error:
        flash(f"Slack authorization failed: {error}", "error")
        org_slug = session.pop("slack_oauth_org_slug", None)
        session.pop("slack_oauth_state", None)
        if org_slug:
            return redirect(url_for("user.organization_settings", org_slug=org_slug))
        return redirect(url_for("user.organizations"))

    state = request_arg("state")
    expected_state = session.pop("slack_oauth_state", None)
    org_slug = session.pop("slack_oauth_org_slug", None)

    if not state or not expected_state or state != expected_state:
        flash("Invalid OAuth state. Please try again.", "error")
        if org_slug:
            return redirect(url_for("user.organization_settings", org_slug=org_slug))
        return redirect(url_for("user.organizations"))

    if not org_slug:
        flash("Missing organization context. Please try again.", "error")
        return redirect(url_for("user.organizations"))

    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)

    code = request_arg("code")
    if not code:
        flash("No authorization code received from Slack.", "error")
        return redirect(url_for("user.organization_settings", org_slug=org_slug))

    # Exchange code for token
    client_id = current_app.config["SLACK_CLIENT_ID"]
    client_secret = current_app.config["SLACK_CLIENT_SECRET"]

    scheme = current_app.config["EXT_PREFERRED_URL_SCHEME"]
    server = current_app.config["EXT_SERVER_NAME"]
    callback_path = url_for("slack_oauth.callback")
    redirect_uri = f"{scheme}://{server}{callback_path}"

    try:
        resp = http_requests.post(
            SLACK_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            timeout=10,
        )
        data = resp.json()
    except http_requests.RequestException:
        flash("Could not reach Slack API. Please try again.", "danger")
        return redirect(url_for("user.organization_settings", org_slug=org_slug))

    if not data.get("ok"):
        error_msg = data.get("error", "unknown error")
        flash(f"Slack OAuth failed: {error_msg}", "danger")
        return redirect(url_for("user.organization_settings", org_slug=org_slug))

    access_token = data.get("access_token")
    team_info = data.get("team", {})
    team_id = team_info.get("id")
    team_name = team_info.get("name")
    bot_user_id = data.get("bot_user_id")

    # Store token in Vault
    vault_path = f"{vault.vault_prefix}/integrations/slack/{organization.id}"
    try:
        vault.vault_connection.write(vault_path, bot_token=access_token)
    except Exception:
        log.exception("Failed to write Slack token to Vault")
        flash("Failed to store credentials securely. Please try again.", "danger")
        return redirect(url_for("user.organization_settings", org_slug=org_slug))

    # Create or update integration record
    integration = organization.slack_integration
    if integration:
        integration.team_id = team_id
        integration.team_name = team_name
        integration.bot_user_id = bot_user_id
        integration.access_token_vault_path = vault_path
        integration.installed_by_user_id = current_user.id
    else:
        integration = SlackIntegration(
            organization_id=organization.id,
            team_id=team_id,
            team_name=team_name,
            bot_user_id=bot_user_id,
            access_token_vault_path=vault_path,
            installed_by_user_id=current_user.id,
        )
        db.session.add(integration)

    db.session.commit()
    flash(
        f'Slack workspace "{team_name}" connected successfully.',
        "success",
    )
    return redirect(url_for("user.organization_settings", org_slug=org_slug))


@slack_oauth_bp.route("/disconnect/<org_slug>", methods=["POST"])
@login_required
def disconnect(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)

    integration = organization.slack_integration
    if integration:
        # Send farewell message and leave channel before removing credentials
        if integration.default_channel_id:
            _send_slack_message(
                integration,
                integration.default_channel_id,
                f"Cabotage alert notifications for *{organization.name}* have been disconnected from this workspace.",
            )
            _slack_leave_channel(integration, integration.default_channel_id)

        # Remove token from Vault
        if integration.access_token_vault_path:
            try:
                vault.vault_connection.delete(integration.access_token_vault_path)
            except Exception:
                log.warning("Failed to delete Slack token from Vault", exc_info=True)

        db.session.delete(integration)
        db.session.commit()
        flash("Slack integration removed.", "success")

    return redirect(url_for("user.organization_settings", org_slug=org_slug))


@slack_oauth_bp.route("/channel/<org_slug>", methods=["POST"])
@login_required
def update_channel(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)

    integration = organization.slack_integration
    if not integration:
        flash("Slack is not connected.", "error")
        return redirect(url_for("user.organization_settings", org_slug=org_slug))

    from flask import request

    channel_id = request.form.get("channel_id") or None
    channel_name = request.form.get("channel_name") or None
    old_channel_id = integration.default_channel_id

    integration.default_channel_id = channel_id
    integration.default_channel_name = channel_name
    db.session.commit()

    if channel_id:
        _slack_join_channel(integration, channel_id)
        if old_channel_id and old_channel_id != channel_id:
            _send_slack_message(
                integration,
                old_channel_id,
                f"Cabotage alert notifications for *{organization.name}* have been moved to <#{channel_id}>.",
            )
            _slack_leave_channel(integration, old_channel_id)
        _send_slack_message(
            integration,
            channel_id,
            f"This channel is now the default notification channel for *{organization.name}*. Alerts that are not disabled or routed to a specific application or environment channel will be sent here.",
        )
        flash(f"Default Slack channel set to #{channel_name}.", "success")
    else:
        if old_channel_id:
            _send_slack_message(
                integration,
                old_channel_id,
                f"Cabotage alert notifications for *{organization.name}* no longer have a default channel.",
            )
            _slack_leave_channel(integration, old_channel_id)
        flash("Default Slack channel cleared.", "success")

    return redirect(url_for("user.organization_settings", org_slug=org_slug))


def _get_slack_token(integration):
    """Read the bot token from Vault. Returns None on failure."""
    if not integration.access_token_vault_path:
        return None
    try:
        secret = vault.vault_connection.read(integration.access_token_vault_path)
        return secret["data"]["bot_token"]
    except Exception:
        log.warning("Failed to read Slack token from Vault", exc_info=True)
        return None


def _slack_join_channel(integration, channel_id):
    """Join a public Slack channel. No-op if already joined or private."""
    token = _get_slack_token(integration)
    if not token:
        return
    try:
        http_requests.post(
            "https://slack.com/api/conversations.join",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel_id},
            timeout=10,
        )
    except http_requests.RequestException:
        log.warning("Failed to join Slack channel %s", channel_id, exc_info=True)


def _slack_leave_channel(integration, channel_id):
    """Leave a Slack channel. Best-effort."""
    token = _get_slack_token(integration)
    if not token:
        return
    try:
        http_requests.post(
            "https://slack.com/api/conversations.leave",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel_id},
            timeout=10,
        )
    except http_requests.RequestException:
        log.warning("Failed to leave Slack channel %s", channel_id, exc_info=True)


def _send_slack_message(integration, channel_id, text):
    """Send a message to a Slack channel. Best-effort — failures are logged."""
    token = _get_slack_token(integration)
    if not token:
        return
    try:
        resp = http_requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel_id, "text": text},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            log.warning("Slack chat.postMessage failed: %s", data.get("error"))
    except http_requests.RequestException:
        log.warning("Failed to send Slack message", exc_info=True)


def get_slack_channels(organization):
    """Fetch available channels from Slack for channel selection UI."""
    integration = organization.slack_integration
    if not integration or not integration.access_token_vault_path:
        return []

    try:
        secret = vault.vault_connection.read(integration.access_token_vault_path)
        token = secret["data"]["bot_token"]
    except Exception:
        log.warning("Failed to read Slack token from Vault", exc_info=True)
        return []

    try:
        resp = http_requests.get(
            "https://slack.com/api/conversations.list",
            headers={"Authorization": f"Bearer {token}"},
            params={"types": "public_channel,private_channel", "limit": 200},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return [
                {"id": ch["id"], "name": ch["name"]}
                for ch in data.get("channels", [])
                if not ch.get("is_archived")
            ]
    except http_requests.RequestException:
        log.warning("Failed to fetch Slack channels", exc_info=True)

    return []


@slack_oauth_bp.route("/channels/<org_slug>")
@login_required
def list_channels(org_slug):
    from flask import jsonify

    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)

    channels = get_slack_channels(organization)
    return jsonify({"channels": channels})


def request_arg(key):
    from flask import request

    return request.args.get(key)


def init_slack_oauth(app):
    if not app.config.get("SLACK_CLIENT_ID"):
        return

    app.register_blueprint(slack_oauth_bp)

    @app.context_processor
    def slack_oauth_context():
        return {"slack_oauth_enabled": True}
