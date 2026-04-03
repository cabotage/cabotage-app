"""Notification dispatch tasks.

Sends and updates Slack/Discord messages for alert state changes and
pipeline events (image build, release, deploy). Messages are tracked
via SentNotification so they can be edited in place as state changes.
"""

import logging
import uuid as _uuid
from datetime import UTC, datetime, timedelta

from celery import shared_task
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from cabotage.server import db
from cabotage.server.integrations.discord_oauth import (
    _send_discord_message,
    _update_discord_message,
)
from cabotage.server.integrations.slack_oauth import (
    _send_slack_message,
    _update_slack_message,
)
from cabotage.server.models.auth import Organization
from cabotage.server.models.notifications import (
    NotificationRoute,
    SentNotification,
)
from cabotage.server.models.projects import (
    Alert,
    Application,
    ApplicationEnvironment,
    Deployment,
    Image,
    Release,
)
from cabotage.utils.github import cabotage_url

log = logging.getLogger(__name__)

# Default cooldown window for repeated alert notifications (seconds).
ALERT_COOLDOWN_SECONDS = 300

# Retry config for send_notification task.
SEND_MAX_RETRIES = 5
SEND_RETRY_BACKOFF = 10  # seconds, doubled each retry

# Maps Alertmanager alertname values to notification type strings.
ALERTNAME_TYPE_MAP = {
    "ResidentDeploymentOOMKilled": "health.oom",
    "ResidentDeploymentCrashLoop": "health.crash_restart",
    "TraefikHighErrorRate": "http.5xx",
    "TraefikHighLatency": "http.latency",
}

# Human-friendly display names for Alertmanager alertnames.
ALERTNAME_DISPLAY = {
    "ResidentDeploymentOOMKilled": "Out of Memory",
    "ResidentDeploymentCrashLoop": "Crash Loop",
    "TraefikHighErrorRate": "High Error Rate",
    "TraefikHighLatency": "High Latency",
}


def _format_app_path(application, app_env):
    org = application.project.organization
    parts = f"{org.slug} / {application.project.slug} / {application.slug}"
    if app_env and app_env.environment:
        parts += f" ({app_env.environment.slug})"
    return parts


COLOR_RED = "#e74c3c"
COLOR_GREEN = "#2ecc71"
COLOR_BLUE = "#3498db"

# Discord colors are decimal integers
DISCORD_RED = 0xE74C3C
DISCORD_GREEN = 0x2ECC71
DISCORD_BLUE = 0x3498DB


def _format_duration(start, end):
    delta = end - start
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _detail_url(application, object_type, object_id):
    path_map = {
        "Image": f"images/{object_id}",
        "Release": f"releases/{object_id}",
        "Deployment": f"deployments/{object_id}",
    }
    path = path_map.get(object_type)
    if path and application:
        return cabotage_url(application, path)
    return None


def _cabotage_branding():
    """Return (icon_url, server_name, base_url) for notification footers."""
    try:
        from flask import current_app

        scheme = current_app.config.get("EXT_PREFERRED_URL_SCHEME", "https")
        server = current_app.config.get("EXT_SERVER_NAME", "localhost")
        base_url = f"{scheme}://{server}"
        return f"{base_url}/static/cabotage-purp.png", server, base_url
    except RuntimeError:
        return "", "", ""


def _build_message(
    title,
    color_hex,
    color_int,
    body_parts,
    links=None,
    error=None,
    slack_extra=None,
    discord_extra=None,
):
    """Build a rich notification payload for Slack, Discord, and plain text.

    Args:
        title: Header line (e.g. "FIRING: OOMKilled")
        color_hex: Slack sidebar color
        color_int: Discord sidebar color (decimal int)
        body_parts: List of body lines (shared across platforms)
        links: Dict of {label: url} — rendered as Slack buttons / Discord button components
        error: Error string (rendered as code block)
        slack_extra: Additional mrkdwn lines appended only to Slack body
        discord_extra: Additional markdown lines appended only to Discord body
    """
    links = links or {}

    # --- Slack: attachment with color bar, buttons via actions block ---
    slack_parts = [f"*{title}*"] + list(body_parts)
    if slack_extra:
        slack_parts.extend(slack_extra)
    if error:
        slack_parts.append(f"```{error}```")

    slack_blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(slack_parts)},
        },
    ]
    if links:
        slack_blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": label},
                        "url": url,
                    }
                    for label, url in links.items()
                ],
            }
        )

    icon_url, server_name, base_url = _cabotage_branding()
    if icon_url:
        slack_blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "image",
                        "image_url": icon_url,
                        "alt_text": "Cabotage",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"<{base_url}|{server_name}>",
                    },
                ],
            }
        )

    slack_attachments = [{"color": color_hex, "blocks": slack_blocks}]

    # --- Discord: embed + button components ---
    discord_parts = list(body_parts)
    if discord_extra:
        discord_parts.extend(discord_extra)
    if error:
        discord_parts.append(f"```{error}```")

    discord_embeds = [
        {
            "title": title,
            "description": "\n".join(discord_parts),
            "color": color_int,
            "footer": {
                "text": server_name or "Cabotage",
                "icon_url": icon_url or None,
            },
        }
    ]

    discord_components = None
    if links:
        buttons = [
            {"type": 2, "style": 5, "label": label, "url": url}
            for label, url in links.items()
        ]
        discord_components = [{"type": 1, "components": buttons}]

    # --- Plain text fallback ---
    fallback_parts = [title] + list(body_parts)
    if error:
        fallback_parts.append(error)

    return {
        "text": "\n".join(fallback_parts),
        "slack_attachments": slack_attachments,
        "discord_embeds": discord_embeds,
        "discord_components": discord_components,
    }


def format_alert_message(alert, application, app_env):
    severity = alert.labels.get("severity", "unknown")
    summary = alert.annotations.get("summary", "")
    app_path = _format_app_path(application, app_env) if application else None
    app_url = cabotage_url(application) if application else None
    display_name = ALERTNAME_DISPLAY.get(alert.alertname, alert.alertname)

    if alert.status == "resolved":
        title = f"\u2705 Resolved: {display_name}"
        color_hex, color_int = COLOR_GREEN, DISCORD_GREEN
    else:
        title = f"\U0001f534 {display_name}"
        color_hex, color_int = COLOR_RED, DISCORD_RED

    body_parts = []
    if app_path:
        body_parts.append(app_path)
    if summary:
        body_parts.append(summary)
    if alert.status == "resolved" and alert.starts_at and alert.ends_at:
        body_parts.append(
            f"Resolved after {_format_duration(alert.starts_at, alert.ends_at)}"
        )
    body_parts.append(f"Severity: `{severity}`")

    links = {}
    if app_url:
        links["View application"] = app_url

    return _build_message(title, color_hex, color_int, body_parts, links=links)


def format_pipeline_message(
    notification_type, app_path, detail, error=None, complete=False, url=None
):
    started_labels = {
        "pipeline.image_build": "Image build",
        "pipeline.release": "Release",
        "pipeline.deploy": "Deploy",
    }
    complete_labels = {
        "pipeline.image_build": "Image built",
        "pipeline.release": "Release created",
        "pipeline.deploy": "Deploy complete",
    }

    if error:
        label = started_labels.get(notification_type, notification_type)
        title = f"\u274c {label} failed"
        color_hex, color_int = COLOR_RED, DISCORD_RED
    elif complete:
        label = complete_labels.get(notification_type, notification_type)
        title = f"\u2705 {label}"
        color_hex, color_int = COLOR_GREEN, DISCORD_GREEN
    else:
        label = started_labels.get(notification_type, notification_type)
        title = f"\U0001f527 {label} started"
        color_hex, color_int = COLOR_BLUE, DISCORD_BLUE

    body_parts = []
    if app_path:
        body_parts.append(app_path)
    if detail:
        body_parts.append(detail)

    links = {}
    if url:
        links["View details"] = url

    return _build_message(
        title, color_hex, color_int, body_parts, links=links, error=error
    )


AUTODEPLOY_STAGES = {
    "image_building": "\u26a1 Auto-deploy: building image...",
    "image_failed": "\u274c Auto-deploy: image build failed",
    "release_building": "\u26a1 Auto-deploy: building release...",
    "release_failed": "\u274c Auto-deploy: release build failed",
    "deploying": "\u26a1 Auto-deploy: deploying...",
    "deploy_failed": "\u274c Auto-deploy: deploy failed",
    "complete": "\u2705 Auto-deploy: complete",
}


def format_autodeploy_message(
    stage,
    app_path,
    initiator=None,
    repo=None,
    short_sha=None,
    commit_url=None,
    error=None,
    image_url=None,
    release_url=None,
    deploy_url=None,
):
    title = AUTODEPLOY_STAGES.get(stage, stage)

    if "failed" in stage:
        color_hex, color_int = COLOR_RED, DISCORD_RED
    elif stage == "complete":
        color_hex, color_int = COLOR_GREEN, DISCORD_GREEN
    else:
        color_hex, color_int = COLOR_BLUE, DISCORD_BLUE

    body_parts = []
    if app_path:
        body_parts.append(app_path)
    if initiator:
        body_parts.append(initiator)

    # Commit line uses platform-specific link syntax
    slack_extra = None
    discord_extra = None
    if repo and short_sha and commit_url:
        slack_extra = [f"<{commit_url}|{repo} @ {short_sha}>"]
        discord_extra = [f"[{repo} @ {short_sha}]({commit_url})"]
    elif repo and short_sha:
        line = f"{repo} @ {short_sha}"
        slack_extra = [line]
        discord_extra = [line]

    links = {}
    if image_url:
        links["View Image"] = image_url
    if release_url:
        links["View Release"] = release_url
    if deploy_url:
        links["View Deploy"] = deploy_url

    return _build_message(
        title,
        color_hex,
        color_int,
        body_parts,
        links=links,
        error=error,
        slack_extra=slack_extra,
        discord_extra=discord_extra,
    )


def dispatch_autodeploy_notification(
    stage,
    image_id,
    application,
    app_env,
    error=None,
    image_url=None,
    release_url=None,
    deploy_url=None,
    image_metadata=None,
):
    """Send or update an auto-deploy notification, keyed by image_id.

    Called from within Celery tasks so runs synchronously (not .delay).
    """
    organization = application.project.organization
    app_path = _format_app_path(application, app_env)

    # Determine initiator and commit info from image_metadata
    metadata = image_metadata or {}
    triggered_by = metadata.get("triggered_by")
    sha = metadata.get("sha")
    repo = application.github_repository

    initiator = None
    short_sha = None
    commit_url = None

    if triggered_by:
        initiator = f"Triggered by: {triggered_by}"
    elif sha:
        initiator = "Triggered by: push"

    if sha:
        short_sha = sha[:7]
        if repo:
            commit_url = f"https://github.com/{repo}/commit/{sha}"

    message = format_autodeploy_message(
        stage,
        app_path,
        initiator=initiator,
        repo=repo,
        short_sha=short_sha,
        commit_url=commit_url,
        error=error,
        image_url=image_url,
        release_url=release_url,
        deploy_url=deploy_url,
    )

    notification_type = "pipeline.deploy"
    targets = resolve_routes(organization, notification_type, application, app_env)
    if error:
        failed_targets = resolve_routes(
            organization, "pipeline.deploy_failed", application, app_env
        )
        seen = set(targets)
        for t in failed_targets:
            if t not in seen:
                targets.append(t)
                seen.add(t)

    for integration, channel_id in targets:
        send_notification.delay(
            str(organization.id),
            integration,
            channel_id,
            "AutoDeploy",
            str(image_id),
            notification_type,
            message,
        )


def resolve_routes(organization, notification_type, application=None, app_env=None):
    """Resolve notification routes for a given event.

    Returns a list of (integration, channel_id) tuples. Falls back to
    the default channel on each connected integration if no routes match.
    """
    routes = NotificationRoute.query.filter(
        NotificationRoute.organization_id == organization.id,
        NotificationRoute.enabled.is_(True),
        NotificationRoute.notification_types.contains([notification_type]),
    ).all()

    matched = []
    for route in routes:
        if route.application_ids and application:
            if str(application.id) not in [str(a) for a in route.application_ids]:
                continue
        elif route.application_ids:
            continue

        if route.environment_ids and app_env and app_env.environment:
            if str(app_env.environment_id) not in [
                str(e) for e in route.environment_ids
            ]:
                continue
        elif route.environment_ids:
            continue

        if route.project_ids and application:
            if str(application.project_id) not in [str(p) for p in route.project_ids]:
                continue
        elif route.project_ids:
            continue

        matched.append((route.integration, route.channel_id))

    seen = set()
    unique = []
    for pair in matched:
        if pair not in seen:
            seen.add(pair)
            unique.append(pair)

    if unique:
        return unique

    defaults = []
    slack = organization.slack_integration
    if slack and slack.default_channel_id:
        defaults.append(("slack", slack.default_channel_id))
    discord = organization.discord_integration
    if discord and discord.default_channel_id:
        defaults.append(("discord", discord.default_channel_id))
    return defaults


@shared_task(
    bind=True,
    max_retries=SEND_MAX_RETRIES,
    default_retry_delay=SEND_RETRY_BACKOFF,
)
def send_notification(
    self,
    organization_id,
    integration,
    channel_id,
    object_type,
    object_id,
    notification_type,
    message,
):
    """Send or update a single notification message. Retries on failure.

    ``message`` is a dict with keys: text (fallback), slack_attachments,
    discord_embeds. A plain string is also accepted for simple messages.
    """
    if isinstance(object_id, str):
        object_id = _uuid.UUID(object_id)

    if isinstance(message, str):
        message = {"text": message}

    text = message.get("text", "")
    slack_attachments = message.get("slack_attachments")
    discord_embeds = message.get("discord_embeds")
    discord_components = message.get("discord_components")

    # Slack renders `text` above attachments, causing duplication.
    # When attachments are present, use text only as the push-notification
    # fallback (not rendered in channel).
    slack_text = "" if slack_attachments else text

    organization = Organization.query.filter_by(id=organization_id).first()
    if not organization:
        log.warning("Organization %s not found for notification", organization_id)
        return

    # Advisory lock keyed on the notification identity. This serializes
    # concurrent workers for the same (object_type, object_id, integration,
    # channel_id) tuple so only one sends while others wait.
    lock_key = hash((object_type, str(object_id), integration, channel_id)) % (2**63)
    db.session.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    existing = SentNotification.query.filter_by(
        object_type=object_type,
        object_id=object_id,
        integration=integration,
        channel_id=channel_id,
    ).first()

    try:
        if existing:
            if integration == "slack":
                slack = organization.slack_integration
                if slack:
                    _update_slack_message(
                        slack,
                        channel_id,
                        existing.external_message_id,
                        slack_text,
                        attachments=slack_attachments,
                    )
            elif integration == "discord":
                _update_discord_message(
                    channel_id,
                    existing.external_message_id,
                    text,
                    embeds=discord_embeds,
                    components=discord_components,
                )
            existing.updated_at = datetime.now(UTC).replace(tzinfo=None)
            db.session.commit()
            return

        external_id = None
        if integration == "slack":
            slack = organization.slack_integration
            if slack:
                external_id = _send_slack_message(
                    slack, channel_id, slack_text, attachments=slack_attachments
                )
        elif integration == "discord":
            external_id = _send_discord_message(
                channel_id, text, embeds=discord_embeds, components=discord_components
            )

        if external_id:
            sent = SentNotification(
                organization_id=organization.id,
                notification_type=notification_type,
                object_type=object_type,
                object_id=object_id,
                integration=integration,
                channel_id=channel_id,
                external_message_id=external_id,
            )
            db.session.add(sent)
            try:
                db.session.commit()
            except IntegrityError:
                # Should not happen with advisory lock, but guard against it
                db.session.rollback()
                return
        elif integration == "slack" and not organization.slack_integration:
            # Integration was removed between dispatch and send — don't retry
            return
        elif integration == "discord" and not organization.discord_integration:
            return
        else:
            # API call returned no ID — transient failure, retry
            raise self.retry(
                exc=RuntimeError(
                    f"Failed to send {integration} notification to {channel_id}"
                )
            )
    except IntegrityError:
        db.session.rollback()
        return
    except Exception as exc:
        raise self.retry(exc=exc)


def _dispatch_alert_notification_impl(alert_id):
    if isinstance(alert_id, str):
        alert_id = _uuid.UUID(alert_id)
    alert = db.session.get(Alert, alert_id)
    if not alert:
        log.warning("Alert %s not found for dispatch", alert_id)
        return

    application = alert.application
    app_env = alert.application_environment

    if not application:
        return

    organization = application.project.organization

    notification_type = ALERTNAME_TYPE_MAP.get(alert.alertname)

    if alert.status != "resolved" and alert.last_notified_at:
        cooldown = timedelta(seconds=ALERT_COOLDOWN_SECONDS)
        if datetime.now(UTC).replace(tzinfo=None) - alert.last_notified_at < cooldown:
            return

    message = format_alert_message(alert, application, app_env)

    if notification_type:
        targets = resolve_routes(organization, notification_type, application, app_env)
    else:
        targets = []
        slack = organization.slack_integration
        if slack and slack.default_channel_id:
            targets.append(("slack", slack.default_channel_id))
        discord = organization.discord_integration
        if discord and discord.default_channel_id:
            targets.append(("discord", discord.default_channel_id))

    for integration, channel_id in targets:
        send_notification.delay(
            str(organization.id),
            integration,
            channel_id,
            "Alert",
            str(alert.id),
            notification_type or "alert",
            message,
        )

    alert.last_notified_at = datetime.now(UTC).replace(tzinfo=None)


@shared_task()
def dispatch_alert_notification(alert_id):
    _dispatch_alert_notification_impl(alert_id)
    db.session.commit()


def _dispatch_pipeline_notification_impl(
    notification_type,
    object_type,
    object_id,
    organization_id,
    application_id,
    app_env_id=None,
    detail=None,
    error=None,
    complete=False,
):
    organization = Organization.query.filter_by(id=organization_id).first()
    if not organization:
        return

    application = (
        Application.query.filter_by(id=application_id).first()
        if application_id
        else None
    )
    app_env = (
        ApplicationEnvironment.query.filter_by(id=app_env_id).first()
        if app_env_id
        else None
    )

    app_path = _format_app_path(application, app_env) if application else None
    url = _detail_url(application, object_type, object_id) if application else None
    message = format_pipeline_message(
        notification_type, app_path, detail, error=error, complete=complete, url=url
    )

    targets = resolve_routes(organization, notification_type, application, app_env)
    if error:
        failed_type = notification_type + "_failed"
        failed_targets = resolve_routes(organization, failed_type, application, app_env)
        seen = set(targets)
        for t in failed_targets:
            if t not in seen:
                targets.append(t)
                seen.add(t)

    for integration, channel_id in targets:
        send_notification.delay(
            str(organization.id),
            integration,
            channel_id,
            object_type,
            str(object_id),
            notification_type,
            message,
        )


@shared_task()
def dispatch_pipeline_notification(
    notification_type,
    object_type,
    object_id,
    organization_id,
    application_id,
    app_env_id=None,
    detail=None,
    error=None,
    complete=False,
):
    _dispatch_pipeline_notification_impl(
        notification_type,
        object_type,
        object_id,
        organization_id,
        application_id,
        app_env_id,
        detail=detail,
        error=error,
        complete=complete,
    )
    db.session.commit()


# Minimum age before reconciler considers a notification stale (seconds).
RECONCILE_STALE_THRESHOLD = 300  # 5 minutes

# Map object_type → (model class, is_terminal, get_state)
_TERMINAL_CHECKS = {
    "Image": (
        Image,
        lambda obj: obj.built or obj.error,
        lambda obj: "complete" if obj.built else ("error" if obj.error else None),
    ),
    "Release": (
        Release,
        lambda obj: obj.built or obj.error,
        lambda obj: "complete" if obj.built else ("error" if obj.error else None),
    ),
    "Deployment": (
        Deployment,
        lambda obj: obj.complete or obj.error,
        lambda obj: "complete" if obj.complete else ("error" if obj.error else None),
    ),
    "Alert": (
        Alert,
        lambda obj: obj.status == "resolved",
        lambda obj: "resolved" if obj.status == "resolved" else None,
    ),
}


@shared_task()
def reconcile_notifications():
    """Find stale notifications whose source objects have reached a terminal
    state and re-dispatch to update the message.

    Only touches notifications that haven't been updated in
    RECONCILE_STALE_THRESHOLD seconds, to avoid stepping on in-flight workers.
    """
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=RECONCILE_STALE_THRESHOLD
    )

    stale = SentNotification.query.filter(
        SentNotification.updated_at < cutoff,
    ).all()

    reconciled = 0
    for sent in stale:
        try:
            if sent.object_type == "AutoDeploy":
                if _reconcile_autodeploy_notification(sent):
                    reconciled += 1
            else:
                if _reconcile_pipeline_notification(sent):
                    reconciled += 1
        except Exception:
            log.warning("Failed to reconcile notification %s", sent.id, exc_info=True)

    if reconciled:
        db.session.commit()
        log.info("Reconciled %d stale notification(s)", reconciled)


def _reconcile_pipeline_notification(sent):
    """Re-dispatch a pipeline/alert notification to its terminal state.

    Returns True if a re-dispatch was sent.
    """
    check = _TERMINAL_CHECKS.get(sent.object_type)
    if not check:
        return False

    model_cls, is_terminal, get_state = check
    obj = model_cls.query.filter_by(id=sent.object_id).first()
    if not obj or not is_terminal(obj):
        return False

    if isinstance(obj, Alert):
        if obj.application:
            message = format_alert_message(
                obj, obj.application, obj.application_environment
            )
            send_notification.delay(
                str(sent.organization_id),
                sent.integration,
                sent.channel_id,
                sent.object_type,
                str(sent.object_id),
                sent.notification_type,
                message,
            )
            return True
        return False

    application = obj.application
    if not application:
        return False

    app_env = obj.application_environment
    ntype = sent.notification_type
    error = obj.error_detail if obj.error else None
    complete = not obj.error

    app_path = _format_app_path(application, app_env)
    url = _detail_url(application, sent.object_type, sent.object_id)
    message = format_pipeline_message(
        ntype, app_path, detail=None, error=error, complete=complete, url=url
    )
    send_notification.delay(
        str(sent.organization_id),
        sent.integration,
        sent.channel_id,
        sent.object_type,
        str(sent.object_id),
        ntype,
        message,
    )
    return True


def _reconcile_autodeploy_notification(sent):
    """Re-dispatch an auto-deploy notification to the correct terminal stage.

    Walks the pipeline chain (image → release → deployment) to find the
    furthest terminal state.

    Returns True if a re-dispatch was sent.
    """
    image = Image.query.filter_by(id=sent.object_id).first()
    if not image or not image.application:
        return False

    app = image.application
    app_env = image.application_environment

    if image.error:
        dispatch_autodeploy_notification(
            "image_failed",
            image.id,
            app,
            app_env,
            error=image.error_detail,
            image_url=cabotage_url(app, f"images/{image.id}"),
            image_metadata=image.image_metadata,
        )
        return True

    if not image.built:
        return False  # still in progress

    # Image built — find the release
    release = Release.query.filter(
        Release.application_id == image.application_id,
        Release.release_metadata["source_image_id"].astext == str(image.id),
    ).first()

    if not release:
        return False  # release not created yet

    if release.error:
        dispatch_autodeploy_notification(
            "release_failed",
            image.id,
            app,
            app_env,
            error=release.error_detail,
            image_url=cabotage_url(app, f"images/{image.id}"),
            release_url=cabotage_url(app, f"releases/{release.id}"),
            image_metadata=image.image_metadata,
        )
        return True

    if not release.built:
        return False  # still building

    # Release built — find the deployment
    deployment = Deployment.query.filter(
        Deployment.application_id == image.application_id,
        Deployment.deploy_metadata["source_image_id"].astext == str(image.id),
    ).first()

    if not deployment:
        return False  # deployment not created yet

    if deployment.error:
        dispatch_autodeploy_notification(
            "deploy_failed",
            image.id,
            app,
            app_env,
            error=deployment.error_detail,
            image_url=cabotage_url(app, f"images/{image.id}"),
            deploy_url=cabotage_url(app, f"deployments/{deployment.id}"),
            image_metadata=image.image_metadata,
        )
        return True

    if deployment.complete:
        dispatch_autodeploy_notification(
            "complete",
            image.id,
            app,
            app_env,
            image_url=cabotage_url(app, f"images/{image.id}"),
            deploy_url=cabotage_url(app, f"deployments/{deployment.id}"),
            image_metadata=image.image_metadata,
        )
        return True

    return False  # still deploying
