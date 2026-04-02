import datetime
import logging
import secrets
from urllib.parse import urlencode

import requests as http_requests
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    request,
    session,
    url_for,
)
from flask_security import current_user, login_required

from cabotage.server import db
from cabotage.server.acl import AdministerOrganizationPermission
from cabotage.server.models.auth import DiscordIntegration, Organization
from cabotage.server.models.notifications import NotificationRoute
from cabotage.server.models.projects import activity_plugin

Activity = activity_plugin.activity_cls

log = logging.getLogger(__name__)

discord_oauth_bp = Blueprint(
    "discord_oauth", __name__, url_prefix="/integrations/discord"
)

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/v10/oauth2/token"  # nosec B105
DISCORD_API_BASE = "https://discord.com/api/v10"


@discord_oauth_bp.route("/connect/<org_slug>")
@login_required
def connect(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)

    client_id = current_app.config.get("DISCORD_CLIENT_ID")
    if not client_id:
        flash("Discord integration is not configured.", "error")
        return redirect(url_for("user.organization_settings", org_slug=org_slug))

    state = secrets.token_urlsafe(32)
    session["discord_oauth_state"] = state
    session["discord_oauth_org_slug"] = org_slug

    scheme = current_app.config["EXT_PREFERRED_URL_SCHEME"]
    server = current_app.config["EXT_SERVER_NAME"]
    callback_path = url_for("discord_oauth.callback")
    redirect_uri = f"{scheme}://{server}{callback_path}"

    params = {
        "client_id": client_id,
        "permissions": "2048",  # Send Messages
        "scope": "bot",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    authorize_url = f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}"
    return redirect(authorize_url)


@discord_oauth_bp.route("/callback")
@login_required
def callback():
    error = request.args.get("error")
    if error:
        error_desc = request.args.get("error_description", error)
        flash(f"Discord authorization failed: {error_desc}", "error")
        org_slug = session.pop("discord_oauth_org_slug", None)
        session.pop("discord_oauth_state", None)
        if org_slug:
            return redirect(url_for("user.organization_settings", org_slug=org_slug))
        return redirect(url_for("user.organizations"))

    state = request.args.get("state")
    expected_state = session.pop("discord_oauth_state", None)
    org_slug = session.pop("discord_oauth_org_slug", None)

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

    code = request.args.get("code")
    guild_id = request.args.get("guild_id")

    if not code:
        flash("No authorization code received from Discord.", "error")
        return redirect(url_for("user.organization_settings", org_slug=org_slug))

    # Exchange code for token to complete the bot add flow
    client_id = current_app.config["DISCORD_CLIENT_ID"]
    client_secret = current_app.config["DISCORD_CLIENT_SECRET"]

    scheme = current_app.config["EXT_PREFERRED_URL_SCHEME"]
    server = current_app.config["EXT_SERVER_NAME"]
    callback_path = url_for("discord_oauth.callback")
    redirect_uri = f"{scheme}://{server}{callback_path}"

    try:
        resp = http_requests.post(
            DISCORD_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        data = resp.json()
    except http_requests.RequestException:
        flash("Could not reach Discord API. Please try again.", "danger")
        return redirect(url_for("user.organization_settings", org_slug=org_slug))

    if "error" in data:
        error_msg = data.get("error_description", data.get("error", "unknown error"))
        flash(f"Discord OAuth failed: {error_msg}", "danger")
        return redirect(url_for("user.organization_settings", org_slug=org_slug))

    # Extract guild info from the response
    guild = data.get("guild", {})
    if not guild_id:
        guild_id = guild.get("id")
    guild_name = guild.get("name")

    if not guild_id:
        flash("Could not determine Discord server. Please try again.", "danger")
        return redirect(url_for("user.organization_settings", org_slug=org_slug))

    # If we didn't get guild_name from token response, fetch it via bot token
    if not guild_name:
        guild_name = _fetch_guild_name(guild_id)

    # Create or update integration record
    integration = organization.discord_integration
    if integration:
        verb = "reauthorize"
        integration.guild_id = guild_id
        integration.guild_name = guild_name
        integration.installed_by_user_id = current_user.id
    else:
        verb = "connect"
        integration = DiscordIntegration(
            organization_id=organization.id,
            guild_id=guild_id,
            guild_name=guild_name,
            installed_by_user_id=current_user.id,
        )
        db.session.add(integration)

    db.session.flush()
    activity = Activity(
        verb=verb,
        object=organization,
        data={
            "user_id": str(current_user.id),
            "action": f"discord_{verb}",
            "guild_name": guild_name,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
    )
    db.session.add(activity)
    db.session.commit()
    flash(
        f'Discord server "{guild_name or guild_id}" connected successfully.',
        "success",
    )
    return redirect(url_for("user.organization_settings", org_slug=org_slug))


@discord_oauth_bp.route("/disconnect/<org_slug>", methods=["POST"])
@login_required
def disconnect(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)

    integration = organization.discord_integration
    if integration:
        if integration.default_channel_id:
            _send_discord_message(
                integration.default_channel_id,
                f"Cabotage alert notifications for **{organization.name}** have been disconnected from this server.",
            )

        # Delete notification routes that targeted this integration
        NotificationRoute.query.filter_by(
            organization_id=organization.id,
            integration="discord",
        ).delete()

        guild_name = integration.guild_name
        db.session.delete(integration)
        db.session.flush()
        activity = Activity(
            verb="disconnect",
            object=organization,
            data={
                "user_id": str(current_user.id),
                "action": "discord_disconnect",
                "guild_name": guild_name,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()
        flash("Discord integration removed.", "success")

    return redirect(url_for("user.organization_settings", org_slug=org_slug))


@discord_oauth_bp.route("/channel/<org_slug>", methods=["POST"])
@login_required
def update_channel(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)

    integration = organization.discord_integration
    if not integration:
        flash("Discord is not connected.", "error")
        return redirect(url_for("user.organization_settings", org_slug=org_slug))

    channel_id = request.form.get("channel_id") or None
    channel_name = request.form.get("channel_name") or None
    old_channel_id = integration.default_channel_id

    integration.default_channel_id = channel_id
    integration.default_channel_name = channel_name
    db.session.commit()

    if channel_id:
        if old_channel_id and old_channel_id != channel_id:
            _send_discord_message(
                old_channel_id,
                f"Cabotage alert notifications for **{organization.name}** have been moved to <#{channel_id}>.",
            )
        _send_discord_message(
            channel_id,
            f"This channel is now the default notification channel for **{organization.name}**. Alerts that are not disabled or routed to a specific application or environment channel will be sent here.",
        )
        flash(f"Default Discord channel set to #{channel_name}.", "success")
    else:
        if old_channel_id:
            _send_discord_message(
                old_channel_id,
                f"Cabotage alert notifications for **{organization.name}** no longer have a default channel.",
            )
        flash("Default Discord channel cleared.", "success")

    return redirect(url_for("user.organization_settings", org_slug=org_slug))


def _send_discord_message(channel_id, text, embeds=None, components=None):
    """Send a message to a Discord channel. Returns the message ID on success."""
    bot_token = current_app.config.get("DISCORD_BOT_TOKEN")
    if not bot_token:
        return None
    try:
        payload = {"content": text}
        if embeds:
            payload["embeds"] = embeds
            payload["content"] = ""  # use embed only, no duplicate text
        if components:
            payload["components"] = components
        resp = http_requests.post(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {bot_token}"},
            json=payload,
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            log.warning(
                "Discord message send failed: %s %s", resp.status_code, resp.text
            )
            return None
        return resp.json().get("id")
    except http_requests.RequestException:
        log.warning("Failed to send Discord message", exc_info=True)
        return None


def _update_discord_message(channel_id, message_id, text, embeds=None, components=None):
    """Update an existing Discord message."""
    bot_token = current_app.config.get("DISCORD_BOT_TOKEN")
    if not bot_token:
        return
    try:
        payload = {"content": text}
        if embeds:
            payload["embeds"] = embeds
            payload["content"] = ""
        if components:
            payload["components"] = components
        resp = http_requests.patch(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}",
            headers={"Authorization": f"Bot {bot_token}"},
            json=payload,
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning(
                "Discord message update failed: %s %s", resp.status_code, resp.text
            )
    except http_requests.RequestException:
        log.warning("Failed to update Discord message", exc_info=True)


def _fetch_guild_name(guild_id):
    """Fetch guild name using the bot token."""
    bot_token = current_app.config.get("DISCORD_BOT_TOKEN")
    if not bot_token:
        return None
    try:
        resp = http_requests.get(
            f"{DISCORD_API_BASE}/guilds/{guild_id}",
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("name")
    except http_requests.RequestException:
        pass
    return None


def get_discord_channels(organization):
    """Fetch available text channels from Discord for channel selection UI."""
    integration = organization.discord_integration
    if not integration:
        return []

    bot_token = current_app.config.get("DISCORD_BOT_TOKEN")
    if not bot_token:
        return []

    try:
        resp = http_requests.get(
            f"{DISCORD_API_BASE}/guilds/{integration.guild_id}/channels",
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            channels = resp.json()
            # Type 0 = text channel
            return [
                {"id": ch["id"], "name": ch["name"]}
                for ch in channels
                if ch.get("type") == 0
            ]
    except http_requests.RequestException:
        log.warning("Failed to fetch Discord channels", exc_info=True)

    return []


@discord_oauth_bp.route("/channels/<org_slug>")
@login_required
def list_channels(org_slug):
    from flask import jsonify

    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)

    channels = get_discord_channels(organization)
    return jsonify({"channels": channels})


def init_discord_oauth(app):
    if not app.config.get("DISCORD_CLIENT_ID"):
        return

    app.register_blueprint(discord_oauth_bp)

    @app.context_processor
    def discord_oauth_context():
        return {"discord_oauth_enabled": True}
