"""Feedback notification dispatch.

Delivers widget submissions to configured incoming webhooks. Separate from
notify.py, which handles org-routed alert and pipeline notifications.
"""

import logging
from urllib.parse import urlsplit

from celery import shared_task
from flask import current_app
import requests

from cabotage.celery.tasks.notify import (
    COLOR_BLUE,
    COLOR_GREEN,
    COLOR_RED,
    DISCORD_BLUE,
    DISCORD_GREEN,
    DISCORD_RED,
    _build_message,
)

log = logging.getLogger(__name__)

KIND_COLORS = {
    "bug": (COLOR_RED, DISCORD_RED),
    "idea": (COLOR_GREEN, DISCORD_GREEN),
    "other": (COLOR_BLUE, DISCORD_BLUE),
}

# Slack caps a section's text at 3000 chars, Discord an embed description at
# 4096. Budget the message body against the tighter of the two.
SLACK_SECTION_LIMIT = 3000


def format_feedback_message(feedback):
    color_hex, color_int = KIND_COLORS.get(feedback.kind, (COLOR_BLUE, DISCORD_BLUE))
    title = f"New feedback: {feedback.kind}"

    user = feedback.user
    context = [f"From: {user.username or user.email if user else 'unknown'}"]
    if feedback.page_url:
        context.append(f"Page: {feedback.page_url}")
    if feedback.endpoint:
        route = feedback.endpoint
        if feedback.view_args:
            route += " " + " ".join(f"{k}={v}" for k, v in feedback.view_args.items())
        context.append(f"Route: {route}")
    meta = " · ".join(v for v in (feedback.viewport, feedback.theme) if v)
    if meta:
        context.append(meta)

    overhead = len(title) + 4 + sum(len(line) + 1 for line in context)
    budget = max(0, SLACK_SECTION_LIMIT - overhead)
    body_parts = [feedback.message[:budget], ""] + context
    return _build_message(title, color_hex, color_int, body_parts)


@shared_task()
def dispatch_feedback_notification(feedback_id):
    """Deliver a feedback submission to any configured incoming webhooks.

    Best-effort: webhook failures are logged, never retried — feedback is
    already persisted and browsable in the admin.
    """
    from cabotage.server.models.feedback import Feedback

    webhooks: list[tuple[str, str]] = []
    for service, key in (
        ("Discord", "FEEDBACK_DISCORD_WEBHOOK_URL"),
        ("Slack", "FEEDBACK_SLACK_WEBHOOK_URL"),
    ):
        url = current_app.config.get(key)
        if url:
            webhooks.append((service, url))
    if not webhooks:
        return

    feedback = Feedback.query.filter_by(id=feedback_id).first()
    if feedback is None:
        log.warning("Feedback %s not found for notification", feedback_id)
        return

    message = format_feedback_message(feedback)
    # Discord rejects footer icon URLs whose host isn't a public domain
    # (e.g. a local-dev EXT_SERVER_NAME like "cabotage-app:8000").
    for embed in message["discord_embeds"]:
        footer = embed.get("footer") or {}
        host = urlsplit(footer.get("icon_url") or "").hostname or ""
        if "." not in host:
            footer.pop("icon_url", None)

    payloads = {
        "Discord": {"embeds": message["discord_embeds"]},
        "Slack": {"text": "", "attachments": message["slack_attachments"]},
    }
    for service, url in webhooks:
        try:
            resp = requests.post(url, json=payloads[service], timeout=5)
            if resp.status_code >= 400:
                log.warning(
                    "%s webhook rejected feedback %s: %s %s",
                    service,
                    feedback_id,
                    resp.status_code,
                    resp.text[:200],
                )
        except requests.RequestException:
            log.warning(
                "Failed to deliver feedback %s to %s webhook", feedback_id, service
            )
