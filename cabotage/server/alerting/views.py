import hmac
import logging
from datetime import datetime

from flask import Blueprint, abort, current_app, jsonify, request

from cabotage.server import db
from cabotage.server.models.projects import (
    Alert,
    Application,
    Project,
)
from cabotage.server.models.auth import Organization

log = logging.getLogger(__name__)

alerting_blueprint = Blueprint("alerting", __name__)


def _validate_bearer_token():
    secret = current_app.config.get("ALERTMANAGER_WEBHOOK_SECRET")
    if not secret:
        log.warning("ALERTMANAGER_WEBHOOK_SECRET not configured, rejecting request")
        return False

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False

    token = auth_header[len("Bearer ") :]
    return hmac.compare_digest(token, secret)


def _parse_alertmanager_timestamp(ts):
    """Parse an Alertmanager timestamp string to a naive UTC datetime."""
    if not ts:
        return None
    # Alertmanager uses RFC3339 timestamps
    # "0001-01-01T00:00:00Z" means unset (still firing)
    if ts.startswith("0001-01-01"):
        return None
    try:
        # Handle both Z and +00:00 suffixes
        ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        # Store as naive UTC
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, TypeError):
        log.warning("Failed to parse timestamp: %s", ts)
        return None


def _resolve_by_slug_labels(labels):
    """Resolve via explicit label_organization/label_project/label_application
    labels injected by the cabotage:resident_deployment_pod recording rule."""
    org_slug = labels.get("label_organization")
    project_slug = labels.get("label_project")
    app_slug = labels.get("label_application")

    if not (org_slug and project_slug and app_slug):
        return None, None

    application = (
        Application.query.join(Project)
        .join(Project.organization)
        .filter(
            Organization.slug == org_slug,
            Project.slug == project_slug,
            Application.slug == app_slug,
            Application.deleted_at.is_(None),
        )
        .first()
    )
    if application:
        return application, application.default_app_env
    return None, None


def _resolve_by_deployment(labels):
    """Resolve via deployment + namespace labels (pod-level alerts).

    deployment = safe_k8s_name(project.k8s_identifier, app.k8s_identifier)
    namespace  = org.k8s_identifier (or org-env for environment-enabled)
    """
    deployment_name = labels.get("deployment")
    namespace = labels.get("namespace")
    if not deployment_name:
        return None, None

    query = (
        Application.query.join(Project)
        .join(Project.organization)
        .filter(
            Application.deleted_at.is_(None),
            (Project.k8s_identifier + "-" + Application.k8s_identifier)
            == deployment_name,
        )
    )
    if namespace:
        query = query.filter(Organization.k8s_identifier == namespace)

    application = query.first()
    if application:
        return application, application.default_app_env
    return None, None


def _resolve_by_traefik_service(labels):
    """Resolve via Traefik service label (ingress-level alerts).

    Traefik router names follow the pattern:
      {namespace}-{ingress_name}-{hostname_sanitized}-{port}@kubernetes{class}

    The ingress name is {resource_prefix}-{process_name}, and resource_prefix
    is safe_k8s_name(project.k8s_identifier, app.k8s_identifier).

    We match by checking that the service label contains the concatenation of
    project.k8s_identifier-app.k8s_identifier (the resource prefix).
    """
    service = labels.get("service", "")
    if not service or "@" not in service:
        return None, None

    # Strip the @kubernetes... suffix
    router_name = service.split("@")[0]

    # Find applications whose resource prefix appears in the router name
    application = (
        Application.query.join(Project)
        .join(Project.organization)
        .filter(
            Application.deleted_at.is_(None),
            db.literal(router_name).contains(
                Project.k8s_identifier + "-" + Application.k8s_identifier
            ),
        )
        .first()
    )
    if application:
        return application, application.default_app_env
    return None, None


def _resolve_application(labels):
    """Try to resolve an Application and ApplicationEnvironment from alert labels.

    Tries in order:
    1. Explicit slug labels (label_organization, label_project, label_application)
    2. Deployment name + namespace (pod-level alerts like OOMKilled, CrashLoop)
    3. Traefik service name (ingress-level alerts like high error rate)
    """
    for resolver in (
        _resolve_by_slug_labels,
        _resolve_by_deployment,
        _resolve_by_traefik_service,
    ):
        application, app_env = resolver(labels)
        if application:
            return application, app_env

    return None, None


@alerting_blueprint.route("/alertmanager/webhooks", methods=["POST"])
def alertmanager_webhook():
    if not _validate_bearer_token():
        abort(403)

    payload = request.get_json(silent=True)
    if not payload:
        abort(400)

    alerts_data = payload.get("alerts", [])
    if not alerts_data:
        return jsonify({"status": "ok", "alerts_processed": 0})

    group_key = payload.get("groupKey")
    receiver = payload.get("receiver")

    alerts_processed = 0
    for alert_data in alerts_data:
        labels = alert_data.get("labels", {})
        alertname = labels.get("alertname", "unknown")
        fingerprint = alert_data.get("fingerprint", "")
        status = alert_data.get("status", "unknown")

        starts_at = _parse_alertmanager_timestamp(alert_data.get("startsAt"))
        ends_at = _parse_alertmanager_timestamp(alert_data.get("endsAt"))

        if not starts_at:
            log.warning("Alert missing startsAt, skipping: %s", fingerprint)
            continue

        application, app_env = _resolve_application(labels)
        app_id = application.id if application else None
        app_env_id = app_env.id if app_env else None

        # Upsert: find existing alert by fingerprint + starts_at
        # (same fingerprint with different starts_at is a new incident)
        existing = Alert.query.filter_by(
            fingerprint=fingerprint,
            starts_at=starts_at,
        ).first()

        if existing:
            existing.status = status
            existing.ends_at = ends_at
            existing.labels = labels
            existing.annotations = alert_data.get("annotations", {})
            if app_id and not existing.application_id:
                existing.application_id = app_id
                existing.application_environment_id = app_env_id
        else:
            alert = Alert(
                fingerprint=fingerprint,
                status=status,
                alertname=alertname,
                labels=labels,
                annotations=alert_data.get("annotations", {}),
                starts_at=starts_at,
                ends_at=ends_at,
                generator_url=alert_data.get("generatorURL"),
                group_key=group_key,
                receiver=receiver,
                application_id=app_id,
                application_environment_id=app_env_id,
            )
            db.session.add(alert)
        alerts_processed += 1

    db.session.commit()
    log.info(
        "Processed alertmanager webhook: %d alerts (%s)",
        alerts_processed,
        payload.get("status", "unknown"),
    )

    return jsonify({"status": "ok", "alerts_processed": alerts_processed})
