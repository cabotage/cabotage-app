"""Org-scoped Grafana provisioning.

This module owns every call into the Grafana *admin* API (org/user/
membership/datasource/dashboard management) plus the pure helpers the web
authorization layer needs (stable logins, sync fingerprints, tenant
lists). It never defines Flask routes.
"""

import hashlib
import logging
import re
import secrets
import uuid
from typing import TYPE_CHECKING, cast
from urllib.parse import quote, urlparse

import requests
from flask import current_app

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.grafana import OrganizationGrafanaIntegration
from cabotage.server.models.projects import Application, Project

if TYPE_CHECKING:
    from cabotage._types.grafana import (
        GrafanaDatasourceJsonData,
        GrafanaDatasourcePayload,
        GrafanaDatasourceSecureJsonData,
        GrafanaOrgCreateResponse,
        GrafanaOrgLookupResponse,
        GrafanaOrgMembership,
        GrafanaUserLookupResponse,
        JSONObject,
        JSONValue,
    )

log = logging.getLogger(__name__)

_ROLE_VIEWER = "Viewer"
_ROLE_EDITOR = "Editor"

_PROMETHEUS_DATASOURCE_UID = "cabotage-prometheus"
_LOKI_DATASOURCE_UID = "cabotage-loki"
_OVERVIEW_DASHBOARD_UID = "cabotage-overview"
_LOGS_DASHBOARD_UID = "cabotage-logs"
_ARCHIVED_FOLDER_UID = "cabotage-archived"
_PROJECT_FOLDER_UID = re.compile(r"p-[0-9a-f]{32}")
_APP_DASHBOARD_UID = re.compile(r"a-[0-9a-f]{32}")
_ORG_FILTERS = {
    "project": '=~"$project"',
    "application": '=~"$application"',
    "environment": '=~"$environment"',
}
_ORG_GROUP = ("project", "application")


class GrafanaIntegrationError(Exception):
    """Raised for operational failures talking to the Grafana admin API.

    Messages are built from method/path/status information only — never
    from response bodies, headers, or the underlying exception text — so
    they are always safe to persist in ``last_error`` or log.
    """


_CONTROL_CHARS = frozenset(chr(c) for c in range(0x20)) | {chr(0x7F)}


def _is_valid_internal_url(value: str | None) -> bool:
    """Whether ``value`` is a plain http(s) URL with a host and nothing else.

    Rejects missing/invalid schemes, missing hosts, any embedded userinfo
    (even an empty username paired with a password), invalid ports,
    query/fragment components, and control characters/backslashes — all of
    which have no business in an internal service URL and could otherwise
    smuggle credentials, routing ambiguity, or injection into logs or
    requests.
    """
    if not value:
        return False
    if any(ch in _CONTROL_CHARS for ch in value) or "\\" in value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False
    if "@" in parsed.netloc:
        return False
    try:
        parsed.port
    except ValueError:
        return False
    if parsed.query or parsed.fragment:
        return False
    return True


def _is_valid_same_origin_path(value: str | None) -> bool:
    """Whether ``value`` is a canonical, absolute, trailing-slash,
    same-origin path like ``/grafana/``.

    Rejects the bare root, missing trailing slash, backslashes,
    percent-encoding (which could decode to ``.``/``..``/``/`` aliases),
    control characters, and empty/``.``/``..`` path segments (which would
    let a prefix match assuming a trailing ``/`` be bypassed).
    """
    if not value:
        return False
    if any(ch in _CONTROL_CHARS for ch in value):
        return False
    if "\\" in value or "%" in value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return False
    if value == "/" or not value.startswith("/") or not value.endswith("/"):
        return False
    segments = value.strip("/").split("/")
    return not any(segment in ("", ".", "..") for segment in segments)


_VALID_EDGES = frozenset({"ingress-nginx", "traefik"})


def grafana_configured() -> bool:
    """Whether Grafana is enabled and its configuration is present and
    well-formed.

    Admin credentials are ordinary app config available to web, worker,
    and beat processes alike: there is no dedicated management worker to
    isolate them to.
    """
    cfg = current_app.config
    if not cfg.get("GRAFANA_ENABLED"):
        return False
    edge: str | None = cfg.get("GRAFANA_EDGE")
    if edge not in _VALID_EDGES:
        return False
    if not cfg.get("GRAFANA_ADMIN_USERNAME") or not cfg.get("GRAFANA_ADMIN_PASSWORD"):
        return False
    if not _is_valid_internal_url(cfg.get("GRAFANA_URL")):
        return False
    if not _is_valid_internal_url(cfg.get("MIMIR_URL")):
        return False
    if not _is_valid_internal_url(cfg.get("LOKI_URL")):
        return False
    return _is_valid_same_origin_path(cfg.get("GRAFANA_PUBLIC_PATH"))


def grafana_user_login(user: User) -> str:
    """Stable Grafana login for a Cabotage user, independent of username/email."""
    return str(user.id)


def _active_members(organization: Organization) -> list[OrganizationMember]:
    """Active organization members only — a deactivated Cabotage user must
    never retain Grafana access or count toward the sync fingerprint.
    """
    return [member for member in organization.members if member.user.active]


def organization_tenants(organization: Organization) -> list[str]:
    """Sorted, deduplicated tenant IDs an organization is authorized to read.

    Always includes the organization's own k8s_identifier plus the
    namespace of every nondeleted environment in every nondeleted project.
    Never includes the infra tenant or anything not owned by this org.
    """
    tenants = {organization.k8s_identifier}
    for project in organization.active_projects:
        for environment in project.active_environments:
            tenants.add(environment.k8s_namespace)
    return sorted(tenants)


def _datasource_urls() -> tuple[str, str]:
    cfg = current_app.config
    mimir_url = str(cfg.get("MIMIR_URL") or "").rstrip("/")
    loki_url = str(cfg.get("LOKI_URL") or "")
    return f"{mimir_url}/prometheus", loki_url


def membership_fingerprint(organization: Organization) -> str:
    """Deterministic fingerprint of active member roles.

    The web auth-request endpoint recomputes and compares this value on
    every request, so adding, removing, deactivating, promoting or demoting
    a member closes access until the next successful reconcile applies the
    change in Grafana. Tenant sets and datasource URLs are deliberately not
    included: every reconcile rewrites the datasources, so a new
    environment becomes queryable within one cycle without locking the
    organization out of Grafana in the meantime.
    """
    members = sorted(
        (str(member.user_id), "admin" if member.admin else "member")
        for member in _active_members(organization)
    )
    payload = ",".join(f"{user_id}:{role}" for user_id, role in members)
    return hashlib.sha256(payload.encode()).hexdigest()


class _AdminClient:
    """Thin wrapper around the Grafana management API using admin credentials."""

    def __init__(self) -> None:
        cfg = current_app.config
        raw_url = cfg.get("GRAFANA_URL")
        if not isinstance(raw_url, str) or not _is_valid_internal_url(raw_url):
            raise GrafanaIntegrationError("GRAFANA_URL is not a valid internal URL")
        self.base_url: str = raw_url.rstrip("/")
        username: str | None = cfg.get("GRAFANA_ADMIN_USERNAME")
        password: str | None = cfg.get("GRAFANA_ADMIN_PASSWORD")
        if not username or not password:
            raise GrafanaIntegrationError(
                "Grafana admin credentials are not configured"
            )
        self.auth: tuple[str, str] = (username, password)
        verify: bool | str | None = cfg.get("GRAFANA_VERIFY")
        self.verify: bool | str = True if verify is None else verify
        timeout: int | None = cfg.get("GRAFANA_TIMEOUT")
        self.timeout: int = timeout or 5

    def request(
        self,
        method: str,
        path: str,
        *,
        org_id: int | None = None,
        json: JSONObject | None = None,
        params: dict[str, str] | None = None,
    ) -> requests.Response:
        headers: dict[str, str] = {}
        if org_id is not None:
            headers["X-Grafana-Org-Id"] = str(org_id)
        try:
            return requests.request(
                method,
                f"{self.base_url}{path}",
                auth=self.auth,
                headers=headers,
                verify=self.verify,
                timeout=self.timeout,
                json=json,
                params=params,
            )
        except requests.RequestException as exc:
            # Never include the original exception text: request libraries
            # sometimes echo the URL (which can carry basic-auth creds) in
            # connection error messages.
            raise GrafanaIntegrationError(
                f"Grafana admin API request failed: {method} {path}"
            ) from exc


def _grafana_org_name(organization: Organization) -> str:
    """Stable Grafana org name keyed on the immutable organization UUID.

    Must never be derived from the mutable slug/name: renaming an
    organization must not orphan its existing Grafana org or spawn a
    duplicate.
    """
    return f"cabotage-{organization.id}"


def _ensure_grafana_org(
    client: _AdminClient,
    organization: Organization,
    integration: OrganizationGrafanaIntegration,
) -> int:
    name = _grafana_org_name(organization)

    if integration.grafana_org_id is not None:
        resp = client.request("GET", f"/api/orgs/{integration.grafana_org_id}")
        if resp.status_code == 200:
            return integration.grafana_org_id
        if resp.status_code != 404:
            # An ambiguous failure (timeout, 5xx, ...) must never be treated
            # as "gone" — that would risk creating a duplicate org under a
            # transient error.
            raise GrafanaIntegrationError(
                f"Failed to verify existing Grafana org (status {resp.status_code})"
            )
        # Confirmed gone: fall through to the stable-name lookup/create below
        # so we self-heal rather than leaving the integration stuck.

    org_id: int
    resp = client.request("GET", f"/api/orgs/name/{quote(name, safe='')}")
    if resp.status_code == 200:
        lookup = cast("GrafanaOrgLookupResponse", resp.json())
        org_id = lookup["id"]
    elif resp.status_code != 404:
        raise GrafanaIntegrationError(
            f"Failed to look up Grafana org (status {resp.status_code})"
        )
    else:
        # Org creation is a privileged management-API call; we never rely on
        # an org-scoped token to create the organization.
        create_resp = client.request("POST", "/api/orgs", json={"name": name})
        if create_resp.status_code not in (200, 201):
            raise GrafanaIntegrationError(
                f"Failed to create Grafana org (status {create_resp.status_code})"
            )
        created = cast("GrafanaOrgCreateResponse", create_resp.json())
        org_id = created["orgId"]

    # Assigned and flushed (not committed) so the row lock held by the
    # caller's SELECT ... FOR UPDATE stays in effect across every remaining
    # reconcile step. If a later step fails and rolls back, the caller's
    # failure handler re-persists this resolved id against a freshly
    # re-locked row, so a retry never recreates (or orphans) this org.
    integration.grafana_org_id = org_id
    db.session.flush()
    return org_id


# Grafana ignores OrgId on user creation unless auto_assign_org is true and
# instead gives each new user a personal org where they are Admin; with
# anonymous access or elevated Viewer/Editor defaults the same isolation
# breaks. Reconcile refuses to provision members unless these hold.
_REQUIRED_GRAFANA_SETTINGS = (
    ("users", "auto_assign_org", "true"),
    ("users", "auto_assign_org_role", "Viewer"),
    ("users", "viewers_can_edit", "false"),
    ("users", "editors_can_admin", "false"),
    ("auth.anonymous", "enabled", "false"),
)


def _require_isolating_settings(client: _AdminClient) -> None:
    resp = client.request("GET", "/api/admin/settings")
    if resp.status_code != 200:
        raise GrafanaIntegrationError(
            f"Failed to read Grafana settings (status {resp.status_code})"
        )
    settings = cast("dict[str, dict[str, str]]", resp.json())
    for section, key, expected in _REQUIRED_GRAFANA_SETTINGS:
        if settings.get(section, {}).get(key) != expected:
            raise GrafanaIntegrationError(
                f"Grafana setting [{section}] {key} must be {expected}"
            )


def _ensure_grafana_user(
    client: _AdminClient, login: str, grafana_org_id: int
) -> tuple[int, bool]:
    """Return ``(user_id, created)``.

    ``created`` is True only when this call just created the user via
    ``OrgId``-pinned creation, which Grafana immediately makes a Viewer
    member of ``grafana_org_id`` — the caller must not also POST an
    org-membership add for that user (Grafana rejects a duplicate
    membership), only PATCH the role if it should be Editor.
    """
    resp = client.request("GET", "/api/users/lookup", params={"loginOrEmail": login})
    if resp.status_code == 200:
        return cast("GrafanaUserLookupResponse", resp.json())["id"], False
    if resp.status_code != 404:
        raise GrafanaIntegrationError(
            f"Failed to look up Grafana user (status {resp.status_code})"
        )

    resp = client.request(
        "POST",
        "/api/admin/users",
        json={
            "name": login,
            "login": login,
            "email": f"{login}@cabotage.invalid",
            # Users only ever authenticate via the trusted auth-proxy
            # header (X-WEBAUTH-USER); this password is never used.
            "password": secrets.token_urlsafe(32),
            # With auto_assign_org enabled, omitting OrgId here creates the
            # user in a personal org where they land as Admin — a
            # privilege escalation (that org is reachable via ?orgId=, and
            # Grafana honors the query param over X-Grafana-Org-Id). Pin
            # the user to this organization's Grafana org from creation;
            # Grafana makes them a Viewer of it immediately, and the
            # caller PATCHes the role if it should be Editor.
            "OrgId": grafana_org_id,
        },
    )
    if resp.status_code not in (200, 201):
        raise GrafanaIntegrationError(
            f"Failed to create Grafana user (status {resp.status_code})"
        )
    return cast("GrafanaUserLookupResponse", resp.json())["id"], True


def _reconcile_users_and_memberships(
    client: _AdminClient, organization: Organization, grafana_org_id: int
) -> None:
    # Member role is always Viewer, admin role is always Editor — never
    # Admin/server admin, regardless of Cabotage admin status.
    desired: dict[str, tuple[User, str]] = {
        grafana_user_login(member.user): (
            member.user,
            _ROLE_EDITOR if member.admin else _ROLE_VIEWER,
        )
        for member in _active_members(organization)
    }

    resp = client.request("GET", f"/api/orgs/{grafana_org_id}/users")
    if resp.status_code != 200:
        raise GrafanaIntegrationError(
            f"Failed to list Grafana org users (status {resp.status_code})"
        )
    existing: dict[str, GrafanaOrgMembership] = {
        entry["login"]: entry
        for entry in cast("list[GrafanaOrgMembership]", resp.json())
    }

    for login, (_user, role) in desired.items():
        current_entry = existing.get(login)
        if current_entry is not None:
            if current_entry.get("role") != role:
                patch_resp = client.request(
                    "PATCH",
                    f"/api/orgs/{grafana_org_id}/users/{current_entry['userId']}",
                    json={"role": role},
                )
                if patch_resp.status_code != 200:
                    raise GrafanaIntegrationError(
                        "Failed to update Grafana org role "
                        f"(status {patch_resp.status_code})"
                    )
            continue

        user_id, created = _ensure_grafana_user(client, login, grafana_org_id)
        if created:
            # Grafana already made them a Viewer member of grafana_org_id
            # via the OrgId-pinned creation; adding them again would fail.
            existing[login] = {"userId": user_id, "login": login, "role": _ROLE_VIEWER}
            if role != _ROLE_VIEWER:
                patch_resp = client.request(
                    "PATCH",
                    f"/api/orgs/{grafana_org_id}/users/{user_id}",
                    json={"role": role},
                )
                if patch_resp.status_code != 200:
                    raise GrafanaIntegrationError(
                        "Failed to update Grafana org role "
                        f"(status {patch_resp.status_code})"
                    )
                existing[login]["role"] = role
            continue

        add_resp = client.request(
            "POST",
            f"/api/orgs/{grafana_org_id}/users",
            json={"loginOrEmail": login, "role": role},
        )
        if add_resp.status_code not in (200, 201):
            raise GrafanaIntegrationError(
                f"Failed to add Grafana org membership (status {add_resp.status_code})"
            )
        existing[login] = {"userId": user_id, "login": login, "role": role}

    # Grafana requires an organization administrator. Keep its management
    # account; customer administrators remain Editors and stale users leave.
    for login, entry in existing.items():
        if login in desired or login == client.auth[0]:
            continue
        del_resp = client.request(
            "DELETE", f"/api/orgs/{grafana_org_id}/users/{entry['userId']}"
        )
        if del_resp.status_code not in (200, 404):
            raise GrafanaIntegrationError(
                "Failed to remove stale Grafana org membership "
                f"(status {del_resp.status_code})"
            )


def _revoke_access(
    client: _AdminClient, integration: OrganizationGrafanaIntegration
) -> None:
    """Cut a disabled/deleted org's members off from Grafana.

    Leaves the Grafana org, its datasources, and its dashboards intact —
    only membership is revoked — so re-enabling later preserves whatever
    the customer built.
    """
    if integration.grafana_org_id is None:
        return
    resp = client.request("GET", f"/api/orgs/{integration.grafana_org_id}/users")
    if resp.status_code == 404:
        return
    if resp.status_code != 200:
        raise GrafanaIntegrationError(
            "Failed to list Grafana org users for revocation "
            f"(status {resp.status_code})"
        )
    for entry in cast("list[GrafanaOrgMembership]", resp.json()):
        if entry["login"] == client.auth[0]:
            continue
        del_resp = client.request(
            "DELETE",
            f"/api/orgs/{integration.grafana_org_id}/users/{entry['userId']}",
        )
        if del_resp.status_code not in (200, 404):
            raise GrafanaIntegrationError(
                "Failed to revoke Grafana org membership "
                f"(status {del_resp.status_code})"
            )


def _load_data_source_ca_cert() -> str | None:
    """Read the CA bundle (if configured) that lets Grafana itself trust
    the Mimir/Loki backends' TLS certificates.
    """
    ca_cert_path: str | None = current_app.config.get(
        "GRAFANA_DATA_SOURCE_CA_CERT_PATH"
    )
    if not ca_cert_path:
        return None
    try:
        with open(ca_cert_path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise GrafanaIntegrationError(
            "Failed to read the configured Grafana datasource CA certificate"
        ) from exc


def _datasource_payload(
    *,
    uid: str,
    name: str,
    ds_type: str,
    url: str,
    tenants: list[str],
    ca_cert: str | None,
) -> GrafanaDatasourcePayload:
    json_data: GrafanaDatasourceJsonData = {"httpHeaderName1": "X-Scope-OrgID"}
    secure_json_data: GrafanaDatasourceSecureJsonData = {
        "httpHeaderValue1": "|".join(tenants)
    }
    if ca_cert:
        json_data["tlsAuthWithCACert"] = True
        secure_json_data["tlsCACert"] = ca_cert
    return {
        "uid": uid,
        "name": name,
        "type": ds_type,
        "url": url,
        "access": "proxy",
        "isDefault": False,
        "readOnly": True,
        "editable": False,
        "jsonData": json_data,
        "secureJsonData": secure_json_data,
    }


def _upsert_datasource(
    client: _AdminClient, grafana_org_id: int, payload: GrafanaDatasourcePayload
) -> None:
    uid = payload["uid"]
    resp = client.request(
        "GET",
        f"/api/datasources/uid/{uid}",
        org_id=grafana_org_id,
    )
    if resp.status_code == 200:
        put_resp = client.request(
            "PUT",
            f"/api/datasources/uid/{uid}",
            org_id=grafana_org_id,
            json=cast("JSONObject", payload),
        )
        if put_resp.status_code != 200:
            raise GrafanaIntegrationError(
                f"Failed to update Grafana datasource {uid} "
                f"(status {put_resp.status_code})"
            )
        return
    if resp.status_code != 404:
        raise GrafanaIntegrationError(
            f"Failed to look up Grafana datasource {uid} (status {resp.status_code})"
        )
    post_resp = client.request(
        "POST",
        "/api/datasources",
        org_id=grafana_org_id,
        json=cast("JSONObject", payload),
    )
    if post_resp.status_code not in (200, 201):
        raise GrafanaIntegrationError(
            f"Failed to create Grafana datasource {uid} "
            f"(status {post_resp.status_code})"
        )


def _reconcile_datasources(
    client: _AdminClient,
    organization: Organization,
    grafana_org_id: int,
) -> None:
    prometheus_url, loki_url = _datasource_urls()
    tenants = organization_tenants(organization)
    ca_cert = _load_data_source_ca_cert()

    _upsert_datasource(
        client,
        grafana_org_id,
        _datasource_payload(
            uid=_PROMETHEUS_DATASOURCE_UID,
            name="Cabotage Metrics",
            ds_type="prometheus",
            url=prometheus_url,
            tenants=tenants,
            ca_cert=ca_cert,
        ),
    )
    _upsert_datasource(
        client,
        grafana_org_id,
        _datasource_payload(
            uid=_LOKI_DATASOURCE_UID,
            name="Cabotage Logs",
            ds_type="loki",
            url=loki_url,
            tenants=tenants,
            ca_cert=ca_cert,
        ),
    )


def _dashboard_variables(
    names: tuple[str, ...],
    *,
    logs: bool = False,
    fixed: dict[str, str] | None = None,
) -> JSONObject:
    """Dropdowns for ``names``; each one is narrowed by the ones before it."""
    datasource: JSONObject = {
        "type": "loki" if logs else "prometheus",
        "uid": _LOKI_DATASOURCE_UID if logs else _PROMETHEUS_DATASOURCE_UID,
    }
    prefix = "" if logs else "label_"
    variables: list[JSONValue] = []
    for index, name in enumerate(names):
        matchers = [f"{prefix}{label}{value}" for label, value in (fixed or {}).items()]
        matchers += [f'{prefix}{prior}=~"${prior}"' for prior in names[:index]]
        if logs:
            selector = "{" + ", ".join(['namespace=~".+"', *matchers]) + "}"
            query = f"label_values({selector},{name})"
        else:
            selector = "{" + ", ".join(matchers) + "}" if matchers else ""
            query = f"label_values(kube_pod_labels{selector},label_{name})"
        variables.append(
            {
                "name": name,
                "label": name.title(),
                "type": "query",
                "datasource": datasource,
                "query": query,
                "includeAll": True,
                "allValue": ".*",
                "multi": True,
                "refresh": 2,
                "sort": 1,
                "current": {"text": "All", "value": "$__all"},
            }
        )
    return {"list": variables}


def _timeseries_panel(
    panel_id: int,
    title: str,
    expression: str,
    unit: str,
    x: int,
    y: int,
    *,
    legend: str,
    logs: bool = False,
    width: int = 12,
) -> JSONObject:
    return {
        "id": panel_id,
        "title": title,
        "type": "timeseries",
        "gridPos": {"h": 7, "w": width, "x": x, "y": y},
        "datasource": {
            "type": "loki" if logs else "prometheus",
            "uid": _LOKI_DATASOURCE_UID if logs else _PROMETHEUS_DATASOURCE_UID,
        },
        "interval": "30s",
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "min": 0,
                "color": {"mode": "palette-classic-by-name"},
                "custom": {
                    "lineWidth": 2,
                    "fillOpacity": 8,
                    "spanNulls": False,
                    "showPoints": "never",
                },
            },
            "overrides": [],
        },
        "options": {
            "legend": {
                "displayMode": "table",
                "placement": "bottom",
                "calcs": ["lastNotNull"],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": [{"expr": expression, "refId": "A", "legendFormat": legend}],
    }


def _logs_panel(panel_id: int, y: int, match: str, *, height: int = 12) -> JSONObject:
    return {
        "id": panel_id,
        "title": "Application logs",
        "description": "Expand a line to inspect its labels and structured fields.",
        "type": "logs",
        "gridPos": {"h": height, "w": 24, "x": 0, "y": y},
        "datasource": {"type": "loki", "uid": _LOKI_DATASOURCE_UID},
        "targets": [
            {
                "expr": f'{{namespace=~".+", {match}}}',
                "refId": "A",
                "queryType": "range",
                "maxLines": 500,
            }
        ],
        "options": {
            "showTime": True,
            "timestampResolution": "ms",
            "showLevel": True,
            "showLabels": True,
            "showCommonLabels": False,
            "showLogContextToggle": True,
            "showControls": True,
            "allowDownload": True,
            "showFieldSelector": True,
            "enableLogDetails": True,
            "detailsMode": "sidebar",
            "enableInfiniteScrolling": True,
            "syntaxHighlighting": True,
            "prettifyLogMessage": True,
            "wrapLogMessage": True,
            "fontSize": "small",
            "dedupStrategy": "none",
            "sortOrder": "Descending",
        },
    }


def _dashboard_link(uid: str, title: str) -> JSONObject:
    public_path: str = current_app.config["GRAFANA_PUBLIC_PATH"]
    return {
        "type": "link",
        "title": title,
        "url": f"{public_path}d/{uid}",
        "includeVars": True,
        "keepTime": True,
    }


def _matchers(filters: dict[str, str], *, prefix: str = "") -> str:
    return ", ".join(f"{prefix}{label}{value}" for label, value in filters.items())


def _workload_panels(
    filters: dict[str, str], group: tuple[str, ...]
) -> list[JSONValue]:
    """CPU, memory, traffic, errors, latency, and logs for the workloads matching
    ``filters`` (Cabotage's project/application/environment labels), one series
    per ``group``.
    """
    match = _matchers(filters)
    by = ",".join(group)
    legend = "/".join(f"{{{{{label}}}}}" for label in group)
    # cAdvisor series only carry namespace/pod; take workload labels from KSM.
    kube_by = ",".join(f"label_{label}" for label in group)
    kube_legend = "/".join(f"{{{{label_{label}}}}}" for label in group)
    pod_labels = (
        f"on(namespace,pod) group_left({kube_by}) max by(namespace,pod,{kube_by})"
        f"(kube_pod_labels{{{_matchers(filters, prefix='label_')}}})"
    )
    containers = 'container!="", container!="POD"'
    requests_rate = f"rate(traefik_service_requests_total{{{match}}}[$__rate_interval])"
    errors_rate = f'rate(traefik_service_requests_total{{{match}, code=~"5.."}}[$__rate_interval])'
    return [
        _timeseries_panel(
            1,
            "CPU · cores",
            f"sum by({kube_by})(rate(container_cpu_usage_seconds_total{{{containers}}}"
            f"[$__rate_interval]) * {pod_labels})",
            "cores",
            0,
            0,
            legend=kube_legend,
        ),
        _timeseries_panel(
            2,
            "Memory · working set",
            f"sum by({kube_by})(container_memory_working_set_bytes{{{containers}}}"
            f" * {pod_labels})",
            "bytes",
            12,
            0,
            legend=kube_legend,
        ),
        _timeseries_panel(
            3,
            "Traffic · requests / second",
            f"sum by({by})({requests_rate})",
            "reqps",
            0,
            7,
            legend=legend,
        ),
        _timeseries_panel(
            4,
            "HTTP errors · 5xx",
            f"sum by({by})({errors_rate}) / sum by({by})({requests_rate})",
            "percentunit",
            12,
            7,
            legend=legend,
        ),
        _timeseries_panel(
            5,
            "Latency · 95th percentile",
            f"histogram_quantile(0.95, sum by({by},le)(rate("
            f"traefik_router_request_duration_seconds_bucket{{{match}}}[$__rate_interval])))",
            "s",
            0,
            14,
            legend=legend,
        ),
        _timeseries_panel(
            6,
            "Logs · lines / second",
            f'sum by({by})(rate({{namespace=~".+", {match}}}[$__interval]))',
            "ops",
            12,
            14,
            legend=legend,
            logs=True,
        ),
        _logs_panel(7, 21, match),
    ]


def _starter_dashboard_payload() -> JSONObject:
    return {
        "uid": _OVERVIEW_DASHBOARD_UID,
        "title": "Cabotage Overview",
        "description": "Resources, traffic, and logs across this organization's apps. Select a project, app, or environment to narrow the view. Empty panels mean no matching samples in the selected time range.",
        "tags": ["cabotage", "applications"],
        "schemaVersion": 39,
        "editable": True,
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "templating": _dashboard_variables(tuple(_ORG_FILTERS)),
        "links": [_dashboard_link(_LOGS_DASHBOARD_UID, "Application logs")],
        "panels": _workload_panels(_ORG_FILTERS, _ORG_GROUP),
    }


def _logs_dashboard_payload() -> JSONObject:
    match = _matchers(_ORG_FILTERS)
    by = ",".join(_ORG_GROUP)
    return {
        "uid": _LOGS_DASHBOARD_UID,
        "title": "Cabotage Logs",
        "description": "Logs across all applications. Filter by project, app, and environment, inspect structured fields, or open log context around an event.",
        "tags": ["cabotage", "logs"],
        "schemaVersion": 39,
        "editable": True,
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "templating": _dashboard_variables(tuple(_ORG_FILTERS), logs=True),
        "links": [_dashboard_link(_OVERVIEW_DASHBOARD_UID, "Application overview")],
        "panels": [
            _timeseries_panel(
                1,
                "Log volume · by application",
                f'sum by({by})(count_over_time({{namespace=~".+", {match}}}[$__interval]))',
                "short",
                0,
                0,
                legend="{{project}}/{{application}}",
                logs=True,
                width=24,
            ),
            _logs_panel(2, 7, match, height=18),
        ],
    }


def project_folder_uid(project: Project) -> str:
    return f"p-{uuid.UUID(str(project.id)).hex}"


def app_dashboard_uid(application: Application) -> str:
    return f"a-{uuid.UUID(str(application.id)).hex}"


def _app_dashboard_payload(application: Application) -> JSONObject:
    project = application.project
    fixed = {
        "project": f'="{project.slug}"',
        "application": f'="{application.slug}"',
    }
    return {
        "uid": app_dashboard_uid(application),
        "title": application.name,
        "description": f"Resources, traffic, and logs for {application.name} in {project.name}, by environment.",
        "tags": ["cabotage", "cabotage-application"],
        "schemaVersion": 39,
        "editable": True,
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "templating": _dashboard_variables(("environment",), fixed=fixed),
        "links": [_dashboard_link(_OVERVIEW_DASHBOARD_UID, "Organization overview")],
        "panels": _workload_panels(
            {**fixed, "environment": '=~"$environment"'}, ("environment",)
        ),
    }


def _reconcile_starter_dashboards(client: _AdminClient, grafana_org_id: int) -> None:
    """Create missing starter dashboards without replacing customer edits."""
    for uid, payload in (
        (_OVERVIEW_DASHBOARD_UID, _starter_dashboard_payload),
        (_LOGS_DASHBOARD_UID, _logs_dashboard_payload),
    ):
        resp = client.request(
            "GET", f"/api/dashboards/uid/{uid}", org_id=grafana_org_id
        )
        if resp.status_code == 200:
            continue
        if resp.status_code != 404:
            raise GrafanaIntegrationError(
                f"Failed to look up starter dashboard (status {resp.status_code})"
            )
        resp = client.request(
            "POST",
            "/api/dashboards/db",
            org_id=grafana_org_id,
            json={
                "dashboard": payload(),
                "overwrite": False,
                "message": "cabotage starter dashboard",
            },
        )
        if resp.status_code not in (200, 201):
            raise GrafanaIntegrationError(
                f"Failed to create starter dashboard (status {resp.status_code})"
            )


def _reconcile_project_dashboards(
    client: _AdminClient, organization: Organization, grafana_org_id: int
) -> None:
    """One folder per project and one dashboard per app.

    Missing ones are created; existing dashboards are never overwritten. Folders
    follow project renames. Deleted projects and apps move to an archive folder
    instead of being deleted, since customers may have added their own panels.
    """

    def check(action: str, resp: requests.Response) -> None:
        if resp.status_code not in (200, 201):
            raise GrafanaIntegrationError(
                f"Failed to {action} (status {resp.status_code})"
            )

    def get(action: str, path: str, params: dict[str, str] | None = None) -> JSONValue:
        resp = client.request("GET", path, org_id=grafana_org_id, params=params)
        check(action, resp)
        return cast("JSONValue", resp.json())

    def send(action: str, method: str, path: str, payload: JSONObject) -> None:
        check(action, client.request(method, path, org_id=grafana_org_id, json=payload))

    folders = {
        str(folder["uid"]): str(folder["title"])
        for folder in cast(
            "list[JSONObject]",
            get("list folders", "/api/folders", {"limit": "1000"}),
        )
    }
    dashboards = {
        str(item["uid"]): str(item.get("folderUid") or "")
        for item in cast(
            "list[JSONObject]",
            get("list dashboards", "/api/search", {"type": "dash-db", "limit": "5000"}),
        )
    }

    project_folders: set[str] = set()
    app_dashboards: set[str] = set()
    for project in organization.active_projects:
        folder_uid = project_folder_uid(project)
        project_folders.add(folder_uid)
        if folder_uid not in folders:
            send(
                "create project folder",
                "POST",
                "/api/folders",
                {"uid": folder_uid, "title": project.name},
            )
        elif folders[folder_uid] != project.name:
            send(
                "rename project folder",
                "PUT",
                f"/api/folders/{folder_uid}",
                {"title": project.name, "overwrite": True},
            )
        for application in project.active_applications:
            uid = app_dashboard_uid(application)
            app_dashboards.add(uid)
            if uid in dashboards:
                continue
            send(
                "create app dashboard",
                "POST",
                "/api/dashboards/db",
                {
                    "dashboard": _app_dashboard_payload(application),
                    "folderUid": folder_uid,
                    "overwrite": False,
                    "message": "cabotage app dashboard",
                },
            )

    def removed_project_folder(uid: str) -> bool:
        return bool(_PROJECT_FOLDER_UID.fullmatch(uid)) and uid not in project_folders

    stale_folders = [uid for uid in folders if removed_project_folder(uid)]
    # Dashboards inside a removed project's folder move along with that folder.
    stale_dashboards = [
        uid
        for uid, folder_uid in dashboards.items()
        if _APP_DASHBOARD_UID.fullmatch(uid)
        and uid not in app_dashboards
        and folder_uid != _ARCHIVED_FOLDER_UID
        and not removed_project_folder(folder_uid)
    ]
    if not stale_folders and not stale_dashboards:
        return
    if _ARCHIVED_FOLDER_UID not in folders:
        send(
            "create archive folder",
            "POST",
            "/api/folders",
            {"uid": _ARCHIVED_FOLDER_UID, "title": "Cabotage archive"},
        )
    for uid in stale_folders:
        send(
            "archive project folder",
            "POST",
            f"/api/folders/{uid}/move",
            {"parentUid": _ARCHIVED_FOLDER_UID},
        )
    for uid in stale_dashboards:
        existing = cast(
            "JSONObject", get("look up app dashboard", f"/api/dashboards/uid/{uid}")
        )
        send(
            "archive app dashboard",
            "POST",
            "/api/dashboards/db",
            {
                "dashboard": existing["dashboard"],
                "folderUid": _ARCHIVED_FOLDER_UID,
                "overwrite": True,
                "message": "cabotage archived app dashboard",
            },
        )


def _persist_reconcile_failure(
    organization_id: str, grafana_org_id: int | None, message: str
) -> None:
    """Record a failed reconcile attempt after a rollback.

    The prior transaction (and the row lock it held) is gone once
    ``db.session.rollback()`` runs, so this re-acquires the lock on a
    freshly queried row before writing anything. Only
    ``grafana_org_id`` (when one was actually resolved before the
    failure), ``membership_fingerprint``, and ``last_error`` are touched —
    never ``enabled`` — so a concurrent disable (or re-enable) of the
    integration by another request is never clobbered.
    """
    integration: OrganizationGrafanaIntegration | None = (
        OrganizationGrafanaIntegration.query.filter_by(organization_id=organization_id)
        .with_for_update()
        .first()
    )
    if integration is None:
        return
    if grafana_org_id is not None:
        integration.grafana_org_id = grafana_org_id
    integration.membership_fingerprint = None
    integration.last_error = message
    db.session.commit()


def reconcile_grafana_organization(organization_id: str) -> None:
    """Bring an organization's Grafana org/users/datasources/dashboards in
    line with its current Cabotage state.

    * A missing integration row is a no-op (nothing to provision).
    * A disabled integration or deleted org only revokes member access;
      the Grafana org, its dashboards, and its datasources are preserved.
    * Any failure clears ``membership_fingerprint`` (never leaves a stale
      "ready" state from a prior success) and records a sanitized
      ``last_error``.
    * ``membership_fingerprint`` is only set after every step above has
      succeeded, so "ready" always means a complete, successful reconcile.
    * Datasources are rewritten on every successful reconcile — including
      the periodic one that runs every organization every 60 seconds —
      so a tenant-set change (a new project/environment) becomes visible
      within one cycle.
    """

    organization = db.session.get(Organization, organization_id)
    if organization is None:
        return

    if not grafana_configured():
        return

    # Row-locked for the rest of this transaction so two concurrent
    # reconciles for the same organization can't race each other into
    # creating duplicate Grafana resources.
    integration: OrganizationGrafanaIntegration | None = (
        OrganizationGrafanaIntegration.query.filter_by(organization_id=organization_id)
        .with_for_update()
        .first()
    )
    if integration is None:
        return

    disabled = not integration.enabled or organization.deleted_at is not None

    grafana_org_id: int | None = None

    try:
        client = _AdminClient()

        if disabled:
            _revoke_access(client, integration)
            integration.membership_fingerprint = None
            integration.last_error = None
            db.session.commit()
            return

        _require_isolating_settings(client)
        grafana_org_id = _ensure_grafana_org(client, organization, integration)
        _reconcile_users_and_memberships(client, organization, grafana_org_id)
        _reconcile_datasources(client, organization, grafana_org_id)
        _reconcile_starter_dashboards(client, grafana_org_id)
        _reconcile_project_dashboards(client, organization, grafana_org_id)

        integration.last_error = None
        # Fingerprinted last, and only on full success: this is what makes
        # the integration "ready" for the web auth-request endpoint.
        integration.membership_fingerprint = membership_fingerprint(organization)
        db.session.commit()
    except GrafanaIntegrationError as exc:
        db.session.rollback()
        _persist_reconcile_failure(organization_id, grafana_org_id, str(exc)[:500])
        raise
    except Exception:
        db.session.rollback()
        # Deliberately no exc_info/traceback here: unexpected exceptions
        # (e.g. a malformed admin API response) could otherwise echo
        # response bodies, headers, or other sensitive detail into logs.
        log.error(
            "Unexpected error reconciling Grafana for organization %s",
            organization_id,
        )
        _persist_reconcile_failure(
            organization_id,
            grafana_org_id,
            "internal error during Grafana reconciliation",
        )
        raise GrafanaIntegrationError(
            "internal error during Grafana reconciliation"
        ) from None
