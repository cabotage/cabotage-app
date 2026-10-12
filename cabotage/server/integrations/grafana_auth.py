"""Cabotage authorization callbacks for the Grafana edge (ingress-nginx
``auth-url``) and the metrics tenant router.
"""

import hmac
import posixpath
import re
import uuid
from typing import TypedDict, cast
from urllib.parse import parse_qs, urlsplit

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    request,
    session,
    url_for,
)
from flask_login.utils import current_user, login_required
from flask_wtf import FlaskForm
from kombu.exceptions import OperationalError
from sqlalchemy.orm import joinedload
from werkzeug.wrappers import Response

from cabotage.server import db
from cabotage.server.integrations.grafana import (
    grafana_configured,
    grafana_user_login,
    membership_fingerprint,
    organization_tenants,
)
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.grafana import OrganizationGrafanaIntegration
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Ingress,
    Project,
)


class _Service(TypedDict):
    name: str
    namespace: str
    application: str
    environment: str
    project: str


class _Tenant(TypedDict):
    organization_id: str
    tenant_id: str
    namespaces: list[str]
    services: list[_Service]


grafana_blueprint = Blueprint("grafana", __name__)
_SESSION_ORGANIZATION = "grafana_organization_id"


def _denied(status: int = 403) -> Response:
    return Response(status=status, headers={"Cache-Control": "no-store"})


@grafana_blueprint.app_context_processor
def grafana_template_context() -> dict[str, bool]:
    return {"grafana_available": grafana_configured()}


_EDGE_HEADERS = {
    "ingress-nginx": ("X-Original-URI", "X-Original-Method"),
    "traefik": ("X-Forwarded-Uri", "X-Forwarded-Method"),
}


def _original_request() -> tuple[str, str, dict[str, list[str]]] | None:
    edge: str = current_app.config.get("GRAFANA_EDGE") or ""
    header_names = _EDGE_HEADERS.get(edge)
    if header_names is None:
        return None
    uri_header, method_header = header_names
    uri = request.headers.get(uri_header, "")
    method = request.headers.get(method_header, "")
    if not uri.startswith("/") or uri.startswith("//") or any(ord(c) < 32 for c in uri):
        return None
    parsed = urlsplit(uri)
    path = parsed.path
    # Reject aliases the edge and upstream could normalize differently.
    if parsed.fragment or "%" in path or "\\" in path or "//" in path:
        return None
    if posixpath.normpath(path) != path.rstrip("/"):
        return None
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
        return None
    return path, method, parse_qs(parsed.query)


def _selected_organization() -> Organization | None:
    selected = session.get(_SESSION_ORGANIZATION)
    try:
        organization_id = uuid.UUID(str(selected))
    except ValueError, TypeError, AttributeError:
        return None
    return db.session.get(Organization, organization_id)


def _member(organization: Organization) -> OrganizationMember | None:
    if not current_user.is_authenticated or not current_user.is_active:
        return None
    membership: OrganizationMember | None = OrganizationMember.query.filter_by(
        organization_id=organization.id, user_id=current_user.id
    ).first()
    return membership


def _ready(organization: Organization) -> bool:
    integration = organization.grafana_integration
    return (
        organization.deleted_at is None
        and integration is not None
        and integration.ready
        and integration.grafana_org_id is not None
        and hmac.compare_digest(
            integration.membership_fingerprint or "",
            membership_fingerprint(organization),
        )
    )


def _grafana_request_allowed(
    path: str, method: str, is_admin: bool, grafana_org_id: int
) -> bool:
    prefix: str = current_app.config["GRAFANA_PUBLIC_PATH"]
    if not path.startswith(prefix):
        return False
    relative = "/" + path[len(prefix) :]
    if relative.startswith("/apis/"):
        # API discovery describes supported versions/resources, not tenant data.
        if re.fullmatch(
            r"/apis/(?:dashboard|folder)\.grafana\.app(?:/v\d+(?:(?:alpha|beta)\d+)?)?/?",
            relative,
        ):
            return method in {"GET", "HEAD"}
        resource = re.fullmatch(
            r"/apis/([^/]+)/([^/]+)/namespaces/(org-\d+)(/.*)?", relative
        )
        # Load-bearing, not just defense in depth: Grafana's app-platform
        # namespace scopes resources to a Grafana org, but nothing else
        # stops a member from querying another org's namespace through
        # their own session if we didn't pin it here ourselves.
        if resource is None or resource[3] != f"org-{grafana_org_id}":
            return False
        group, suffix = resource[1], resource[4] or ""
        if group == "features.grafana.app":
            return method == "POST" and suffix == "/ofrep/v1/evaluate/flags"
        if method in {"GET", "HEAD"}:
            return True
        managed_resources = {
            "dashboard.grafana.app": "/dashboards",
            "folder.grafana.app": "/folders",
        }
        root = managed_resources.get(group)
        return (
            is_admin
            and root is not None
            and (suffix == root or suffix.startswith(root + "/"))
        )

    # Classic REST API / UI surface: reads are otherwise allowed and
    # Grafana's own RBAC enforces Viewer/Editor on anything that slips
    # through. These boundaries can never be delegated to a stale or
    # forged Grafana membership, so they stay denied regardless of role.
    denylist = (
        "/admin",
        "/api/admin",
        "/login",
        "/logout",
        "/signup",
        "/invite",
        "/api/orgs",
        "/api/teams",
        "/api/auth",
        "/api/serviceaccounts",
        "/api/user/using",
        "/api/alertmanager",
        "/api/alerting",
        "/api/alert-notifications",
        "/api/notifications",
        "/api/ruler",
        "/api/v1/provisioning",
        "/api/snapshots",
        "/api/public/dashboards",
        "/public-dashboards",
        "/api/live",
    )
    if any(relative == item or relative.startswith(item + "/") for item in denylist):
        return False
    if relative.startswith("/api/access-control") and method not in {"GET", "HEAD"}:
        return False
    if relative == "/api/plugins" or relative.startswith("/api/plugins/"):
        return method in {"GET", "HEAD"}
    if relative == "/api/datasources" or relative.startswith("/api/datasources/"):
        # Query/proxy/resource calls against an existing datasource are data
        # access, not administration; Grafana's own RBAC governs them.
        if re.fullmatch(
            r"/api/datasources/proxy/(?:uid/)?[^/]+(?:/.*)?", relative
        ) or re.fullmatch(
            r"/api/datasources/(?:uid/)?[^/]+/resources(?:/.*)?", relative
        ):
            return True
        # Load-bearing, not just defense in depth: a member who could
        # create, edit or delete a datasource (by uid, numeric id or name)
        # could point it anywhere, including another tenant's X-Scope-OrgID,
        # so every other write stays operator-only regardless of role.
        return method in {"GET", "HEAD"}
    if relative == "/api/org" or relative.startswith("/api/org/"):
        return method in {"GET", "HEAD"}
    if relative == "/api/ds/query":
        return method == "POST"
    if relative == "/api/frontend-metrics":
        return method == "POST"
    if relative == "/api/user/preferences" or relative.startswith(
        "/api/user/preferences/"
    ):
        return True
    if relative == "/api/user/stars" or relative.startswith("/api/user/stars/"):
        return True
    if method in {"GET", "HEAD"}:
        return True
    if not is_admin:
        return False
    # Outbound notification targets remain operator-controlled, like datasources.
    editor_paths = (
        "/api/dashboards",
        "/api/folders",
        "/api/annotations",
    )
    return any(
        relative == item or relative.startswith(item + "/") for item in editor_paths
    )


@grafana_blueprint.get("/grafana-integration/auth")
def auth() -> Response:
    if not grafana_configured():
        return _denied()
    if not current_user.is_authenticated or not current_user.is_active:
        return _denied(401)
    original = _original_request()
    organization = _selected_organization()
    if original is None or organization is None:
        return _denied()
    membership = _member(organization)
    if membership is None or not _ready(organization):
        return _denied()
    path, method, query = original
    integration = organization.grafana_integration
    if integration is None:
        return _denied()
    # Load-bearing, not just defense in depth: Grafana honors an ?orgId=
    # query param over X-Grafana-Org-Id, so without this pin a member
    # could switch Grafana's active org for the request to any org they
    # (or a stale/forged header) can reach, bypassing the membership check
    # above entirely.
    if any(
        value != str(integration.grafana_org_id) for value in query.get("orgId", [])
    ):
        return _denied()
    if integration.grafana_org_id is None or not _grafana_request_allowed(
        path, method, membership.admin, integration.grafana_org_id
    ):
        return _denied()
    return Response(
        status=204,
        headers={
            "Cache-Control": "no-store",
            "X-WEBAUTH-USER": grafana_user_login(cast(User, current_user)),
            "X-Grafana-Org-Id": str(integration.grafana_org_id),
        },
    )


@grafana_blueprint.get("/grafana-integration/tenants")
def tenants() -> Response:
    expected: str | None = current_app.config.get("MONITORING_TENANT_REGISTRY_KEY")
    supplied = request.headers.get("X-Tenant-Registry-Key", "")
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        return _denied()

    from cabotage.server.user.views import (
        _collect_traefik_svc_names,
        _compute_observe_namespace,
        _compute_observe_prefix,
    )

    def namespace_for(enrollment: ApplicationEnvironment) -> str:
        return _compute_observe_namespace(enrollment.application, enrollment)

    def prefix_for(enrollment: ApplicationEnvironment) -> str:
        return _compute_observe_prefix(enrollment.application)

    organizations: list[Organization] = (
        Organization.query.filter(Organization.deleted_at.is_(None))
        .options(
            joinedload(Organization.projects).joinedload(Project.project_environments)
        )
        .all()
    )
    by_org: dict[uuid.UUID, _Tenant] = {
        org.id: {
            "organization_id": str(org.id),
            "tenant_id": org.k8s_identifier,
            "namespaces": organization_tenants(org),
            "services": [],
        }
        for org in organizations
    }
    app_envs: list[ApplicationEnvironment] = (
        ApplicationEnvironment.query.join(
            Application, ApplicationEnvironment.application_id == Application.id
        )
        .join(Project, Application.project_id == Project.id)
        .join(Environment, ApplicationEnvironment.environment_id == Environment.id)
        .filter(
            Project.organization_id.in_(list(by_org)),
            Project.deleted_at.is_(None),
            Application.deleted_at.is_(None),
            Environment.deleted_at.is_(None),
            ApplicationEnvironment.deleted_at.is_(None),
        )
        .options(
            joinedload(ApplicationEnvironment.application)
            .joinedload(Application.project)
            .joinedload(Project.organization),
            joinedload(ApplicationEnvironment.environment),
            joinedload(ApplicationEnvironment.ingresses).joinedload(Ingress.paths),
        )
        .all()
    )
    for app_env in app_envs:
        namespace = _compute_observe_namespace(app_env.application, app_env)
        organization_id = app_env.application.project.organization_id
        if organization_id is None:
            continue
        payload = by_org[organization_id]
        if namespace not in payload["namespaces"]:
            continue
        names = _collect_traefik_svc_names(
            [app_env],
            namespace_for,
            prefix_for,
        )
        payload["services"].extend(
            {
                "name": name,
                "namespace": namespace,
                "application": app_env.application.slug,
                "environment": app_env.environment.slug,
                "project": app_env.application.project.slug,
            }
            for name in sorted(names)
        )
    response = jsonify({"tenants": list(by_org.values())})
    response.headers["Cache-Control"] = "no-store"
    return response


@grafana_blueprint.post("/organizations/<org_slug>/grafana/open")
@login_required
def open_grafana(org_slug: str) -> Response:
    organization: Organization = (
        Organization.query.filter_by(slug=org_slug)
        .filter(Organization.deleted_at.is_(None))
        .first_or_404()
    )
    if _member(organization) is None or not FlaskForm().validate_on_submit():
        return _denied()
    if not grafana_configured() or not _ready(organization):
        flash(
            "Grafana is not ready. Ask an organization admin to check its settings.",
            "warning",
        )
        return redirect(url_for("user.organization", org_slug=org_slug))
    session[_SESSION_ORGANIZATION] = str(organization.id)
    public_path: str = current_app.config["GRAFANA_PUBLIC_PATH"]
    return redirect(public_path + "d/cabotage-overview")


@grafana_blueprint.post("/organizations/<org_slug>/grafana/settings")
@login_required
def settings(org_slug: str) -> Response:
    organization: Organization = (
        Organization.query.filter_by(slug=org_slug)
        .filter(Organization.deleted_at.is_(None))
        .first_or_404()
    )
    membership = _member(organization)
    if (
        membership is None
        or not membership.admin
        or not FlaskForm().validate_on_submit()
    ):
        return _denied()
    if not grafana_configured():
        return _denied()
    action = request.form.get("action")
    if action not in {"enable", "disable", "sync"}:
        return _denied(400)
    integration: OrganizationGrafanaIntegration | None = (
        organization.grafana_integration
    )
    if integration is None:
        if action != "enable":
            return _denied(400)
        integration = OrganizationGrafanaIntegration(organization=organization)
        db.session.add(integration)
    if action != "sync":
        integration.enabled = action == "enable"
    integration.membership_fingerprint = None
    integration.last_error = None
    db.session.commit()
    from cabotage.celery.tasks.grafana import enqueue_reconcile_organization

    try:
        enqueue_reconcile_organization(str(organization.id))
    except OperationalError:
        # Access is already revoked/pending in the database; the periodic job retries.
        flash(
            "Grafana settings saved, but synchronization could not be queued. The scheduled synchronization will retry.",
            "warning",
        )
    else:
        flash(
            "Grafana disabled."
            if action == "disable"
            else "Grafana synchronization queued.",
            "success",
        )
    return redirect(url_for("user.organization_settings", org_slug=org_slug))
