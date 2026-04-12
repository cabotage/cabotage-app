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
from cabotage.server.models.utils import safe_k8s_name

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


def _resolve_app_env_from_namespace(application, namespace):
    """Try to resolve a specific ApplicationEnvironment from the namespace.

    Environment-enabled apps use safe_k8s_name(org.k8s_identifier,
    env.k8s_identifier) as their namespace. If the namespace matches that
    pattern for one of the app's environments, return that AppEnv. Otherwise
    fall back to default_app_env.

    Returns (app_env, matched) where matched is True if the namespace was
    positively matched (org-only or org-env), False if we fell through.
    """
    if not namespace:
        return application.default_app_env, False

    org_k8s = application.project.organization.k8s_identifier

    # Simple case: namespace is just the org identifier
    if namespace == org_k8s:
        return application.default_app_env, True

    # Check each active app env to see if its environment produces this namespace
    for app_env in application.active_application_environments:
        if app_env.k8s_identifier is None:
            continue
        env_namespace = safe_k8s_name(org_k8s, app_env.environment.k8s_identifier)
        if namespace == env_namespace:
            return app_env, True

    return application.default_app_env, False


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
        namespace = labels.get("namespace")
        app_env, _matched = _resolve_app_env_from_namespace(application, namespace)
        return application, app_env
    return None, None


def _resolve_by_deployment(labels):
    """Resolve via deployment + namespace labels (pod-level alerts).

    deployment = safe_k8s_name(project.k8s_identifier, app.k8s_identifier)
    namespace  = org.k8s_identifier (or safe_k8s_name(org, env) for
                 environment-enabled apps)
    """
    deployment_name = labels.get("deployment")
    namespace = labels.get("namespace")
    if not deployment_name:
        return None, None

    # Try each possible split of deployment_name into (project_k8s, app_k8s)
    # and query with exact column equality (index-friendly). This avoids a
    # SQL concat expression which prevents index usage.
    parts = deployment_name.split("-")
    for i in range(1, len(parts)):
        proj_k8s = "-".join(parts[:i])
        app_k8s = "-".join(parts[i:])

        query = (
            Application.query.join(Project)
            .join(Project.organization)
            .filter(
                Application.deleted_at.is_(None),
                Project.k8s_identifier == proj_k8s,
                Application.k8s_identifier == app_k8s,
            )
        )

        if namespace:
            for application in query.all():
                app_env, matched = _resolve_app_env_from_namespace(
                    application, namespace
                )
                if matched:
                    return application, app_env
        else:
            application = query.first()
            if application:
                return application, application.default_app_env

    return None, None


def _resolve_by_traefik_service(labels):
    """Resolve via Traefik service label (ingress-level alerts).

    Traefik router names follow the pattern:
      {namespace}-{resource_prefix}-{ingress}-{resource_prefix}-{process}-{port}@kubernetes{class}

    The resource_prefix is safe_k8s_name(project.k8s_identifier, app.k8s_identifier).

    We match using hyphen-delimited boundaries to avoid false positives where
    one app's k8s_identifier is a prefix of another's (e.g. "foo" vs "foobar").
    """
    service = labels.get("service", "")
    if not service or "@" not in service:
        return None, None

    router_name = service.split("@")[0]

    # Wrap with delimiters so "proj-foo" won't match "proj-foobar"
    resource_prefix = Project.k8s_identifier + "-" + Application.k8s_identifier
    application = (
        Application.query.join(Project)
        .join(Project.organization)
        .filter(
            Application.deleted_at.is_(None),
            db.literal(f"-{router_name}-").contains(
                db.func.concat("-", resource_prefix, "-")
            ),
        )
        .first()
    )
    if application:
        namespace = labels.get("namespace")
        app_env, _matched = _resolve_app_env_from_namespace(application, namespace)
        return application, app_env
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

    Returns (processed, dispatch_id) where processed is True if the alert
    was handled, and dispatch_id is the alert ID to notify (or None).
    """
    if not starts_at:
        log.warning("Alert missing startsAt, skipping: %s", fingerprint)
        return False, None

    application, app_env = resolve_application(labels)
    app_id = application.id if application else None
    app_env_id = app_env.id if app_env else None

    existing = Alert.query.filter_by(
        fingerprint=fingerprint,
        starts_at=starts_at,
    ).first()

    if existing:
        if existing.status == "resolved":
            return True, None
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
            return True, existing.id
        if was_firing and status == "firing" and not existing.last_notified_at:
            return True, existing.id
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
            return True, alert.id

    return True, None
