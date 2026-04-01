"""Shared alert ingestion logic used by both the webhook endpoint and
the reconciliation task."""

import logging
from datetime import UTC, datetime

from cabotage.server import db
from cabotage.server.models.projects import (
    Alert,
    Application,
    Project,
    activity_plugin,
)
from cabotage.server.models.auth import Organization

log = logging.getLogger(__name__)

Activity = activity_plugin.activity_cls


def _record_activity(verb, alert, application=None):
    """Record an Activity entry for an alert state change."""
    data = {
        "action": f"alert_{verb}",
        "alertname": alert.alertname,
        "fingerprint": alert.fingerprint,
        "status": alert.status,
        "severity": alert.labels.get("severity", "unknown"),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    summary = alert.annotations.get("summary")
    if summary:
        data["summary"] = summary

    target = application or alert
    activity = Activity(
        verb=verb,
        object=target,
        data=data,
    )
    db.session.add(activity)


def parse_alertmanager_timestamp(ts):
    """Parse an Alertmanager timestamp string to a naive UTC datetime."""
    if not ts:
        return None
    # "0001-01-01T00:00:00Z" means unset (still firing)
    if ts.startswith("0001-01-01"):
        return None
    try:
        ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
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

    router_name = service.split("@")[0]

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


def resolve_application(labels):
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


def upsert_alert(
    *,
    fingerprint,
    status,
    alertname,
    labels,
    annotations,
    starts_at,
    ends_at,
    generator_url=None,
    group_key=None,
    receiver=None,
):
    """Upsert an alert by (fingerprint, starts_at).

    Returns True if the alert was processed, False if skipped (e.g. missing starts_at).
    Resolved alerts are never reopened.
    """
    if not starts_at:
        log.warning("Alert missing startsAt, skipping: %s", fingerprint)
        return False

    application, app_env = resolve_application(labels)
    app_id = application.id if application else None
    app_env_id = app_env.id if app_env else None

    existing = Alert.query.filter_by(
        fingerprint=fingerprint,
        starts_at=starts_at,
    ).first()

    if existing:
        if existing.status == "resolved":
            return True
        was_firing = existing.status == "firing"
        existing.status = status
        existing.ends_at = ends_at
        existing.labels = labels
        existing.annotations = annotations
        if app_id and not existing.application_id:
            existing.application_id = app_id
            existing.application_environment_id = app_env_id
        if was_firing and status == "resolved":
            _record_activity("resolved", existing, application)
    else:
        alert = Alert(
            fingerprint=fingerprint,
            status=status,
            alertname=alertname,
            labels=labels,
            annotations=annotations,
            starts_at=starts_at,
            ends_at=ends_at,
            generator_url=generator_url,
            group_key=group_key,
            receiver=receiver,
            application_id=app_id,
            application_environment_id=app_env_id,
        )
        db.session.add(alert)
        if status == "firing":
            db.session.flush()  # ensure alert has an id for generic_relationship
            _record_activity("firing", alert, application)

    return True
