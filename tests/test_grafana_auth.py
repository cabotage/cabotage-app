"""Consumer-visible authorization boundaries at the Grafana gateway callbacks."""

import datetime
import time
import uuid
from unittest.mock import patch

import pytest
from flask import g
from flask_security import hash_password
from kombu.exceptions import OperationalError

from cabotage.server import db
from cabotage.server.integrations.grafana import (
    app_dashboard_uid,
    membership_fingerprint,
    project_folder_uid,
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
from cabotage.server.wsgi import app


@pytest.fixture
def gateway(monkeypatch):
    for key, value in {
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "REQUIRE_MFA": False,
        "GRAFANA_ENABLED": True,
        "GRAFANA_EDGE": "ingress-nginx",
        "GRAFANA_URL": "http://grafana:3000",
        "GRAFANA_PUBLIC_PATH": "/grafana/",
        "GRAFANA_ADMIN_USERNAME": "admin",
        "GRAFANA_ADMIN_PASSWORD": "admin",  # nosec B105
        "MIMIR_URL": "http://mimir:8080",
        "LOKI_URL": "http://loki:3100",
        "MONITORING_TENANT_REGISTRY_KEY": "test-registry-key",  # nosec B105
    }.items():
        monkeypatch.setitem(app.config, key, value)
    with app.app_context(), patch.object(db.session, "commit", db.session.flush):
        user = User(
            username=f"grafana-{uuid.uuid4().hex}",
            email=f"{uuid.uuid4().hex}@example.com",
            password=hash_password("password123"),
            active=True,
            fs_uniquifier=uuid.uuid4().hex,
        )
        organizations = [
            Organization(name=f"Gateway {uuid.uuid4().hex}") for _ in range(2)
        ]
        db.session.add_all([user, *organizations])
        db.session.flush()
        member = OrganizationMember(
            organization=organizations[0], user=user, admin=False
        )
        integration = OrganizationGrafanaIntegration(
            organization=organizations[0], enabled=True, grafana_org_id=7
        )
        other_integration = OrganizationGrafanaIntegration(
            organization=organizations[1], enabled=True, grafana_org_id=8
        )
        db.session.add_all([member, integration, other_integration])
        project = Project(
            name="Demo", organization=organizations[0], environments_enabled=True
        )
        db.session.add(project)
        db.session.flush()
        environment = Environment(name="Production", project=project)
        db.session.add(environment)
        db.session.flush()
        integration.membership_fingerprint = membership_fingerprint(organizations[0])
        client = app.test_client()
        with client.session_transaction() as session:
            session.update(
                {
                    "_user_id": user.fs_uniquifier,
                    "_fresh": True,
                    "fs_cc": "set",
                    "fs_paa": time.time(),
                    "identity.id": user.id,
                    "identity.auth_type": "session",
                    "grafana_organization_id": str(organizations[0].id),
                }
            )
        try:
            yield client, user, member, integration, other_integration, environment
        finally:
            db.session.rollback()


def _auth(client, path="/grafana/api/search", method="GET", **extra):
    return client.get(
        "/grafana-integration/auth",
        headers={
            "X-Original-URI": path,
            "X-Original-Method": method,
            **extra,
        },
    )


def _tenants(client, key=None):
    headers = {}
    if key is not None:
        headers["X-Tenant-Registry-Key"] = key
    return client.get("/grafana-integration/tenants", headers=headers)


def test_forged_identity_and_tenant_headers_do_not_select_another_org(gateway):
    client, user, _, integration, _, _ = gateway
    response = _auth(
        client,
        **{
            "X-WEBAUTH-USER": "administrator",
            "X-Grafana-Org-Id": "8",
            "X-WEBAUTH-ROLE": "Admin",
        },
    )
    assert response.status_code == 204
    assert response.headers["X-WEBAUTH-USER"] == str(user.id)
    assert response.headers["X-Grafana-Org-Id"] == "7"
    assert "X-WEBAUTH-ROLE" not in response.headers
    assert "Authorization" not in response.headers
    assert "Cookie" not in response.headers
    assert set(response.headers.keys()) >= {
        "X-WEBAUTH-USER",
        "X-Grafana-Org-Id",
        "Cache-Control",
    }


@pytest.mark.parametrize(
    "path,method",
    [
        ("/grafana/api/user/using/8", "POST"),
        ("/grafana/api/orgs", "POST"),
        ("/grafana/api/admin/users", "POST"),
        ("/grafana/api/org/users", "POST"),
        ("/grafana/api/datasources", "POST"),
        ("/grafana/api/datasources/uid/cabotage-prometheus", "PUT"),
        ("/grafana/api/datasources/uid/cabotage-prometheus", "DELETE"),
        ("/grafana/api/datasources/42", "PUT"),
        ("/grafana/api/datasources/name/cabotage-prometheus", "DELETE"),
        ("/grafana/api/datasources/uid/cabotage-prometheus/correlations", "POST"),
        ("/grafana/api/serviceaccounts", "POST"),
        ("/grafana/api/snapshots", "POST"),
        ("/grafana/api/v1/provisioning/contact-points", "POST"),
        ("/grafana/api/alertmanager/grafana/api/v2/silences", "POST"),
        ("/grafana/api/ruler/grafana/api/v1/rules", "POST"),
        ("/grafana/api/search?orgId=8", "GET"),
        ("/grafana/api/%64atasources", "POST"),
        ("/grafana/api/dashboards/../orgs", "POST"),
        ("/grafana/api/live/ws", "GET"),
        ("/grafana/apis/dashboard.grafana.app/", "POST"),
        ("/grafana/apis/dashboard.grafana.app/v1beta1/dashboards", "GET"),
        (
            "/grafana/apis/dashboard.grafana.app/v1beta1/namespaces/org-8/dashboards",
            "GET",
        ),
        (
            "/grafana/apis/features.grafana.app/v0alpha1/namespaces/org-8/ofrep/v1/evaluate/flags",
            "POST",
        ),
        ("/grafana/apis/iam.grafana.app/v0alpha1/namespaces/org-7/roles", "POST"),
        (
            "/grafana/apis/datasource.grafana.app/v1beta1/namespaces/org-7/datasources",
            "PUT",
        ),
    ],
)
def test_editors_cannot_escape_managed_org_or_datasource_boundary(
    gateway, path, method
):
    client, _, member, integration, _, _ = gateway
    member.admin = True
    integration.membership_fingerprint = membership_fingerprint(
        integration.organization
    )
    assert _auth(client, path, method).status_code == 403


def test_viewer_query_allowed_but_dashboard_write_requires_synced_editor(gateway):
    client, _, member, integration, _, _ = gateway
    assert _auth(client, "/grafana/api/ds/query", "POST").status_code == 204
    assert _auth(client, "/grafana/api/dashboards/db", "POST").status_code == 403
    member.admin = True
    # A changed Cabotage role alone does not bypass pending Grafana synchronization.
    assert _auth(client, "/grafana/api/dashboards/db", "POST").status_code == 403
    integration.membership_fingerprint = membership_fingerprint(
        integration.organization
    )
    assert _auth(client, "/grafana/api/dashboards/db", "POST").status_code == 204


def test_datasource_proxy_and_resource_calls_stay_allowed_for_viewers(gateway):
    client, _, _, _, _, _ = gateway
    assert (
        _auth(
            client,
            "/grafana/api/datasources/uid/cabotage-prometheus/resources/api/v1/labels",
            "POST",
        ).status_code
        == 204
    )
    assert _auth(client, "/grafana/api/frontend-metrics", "POST").status_code == 204
    assert _auth(client, "/grafana/api/user/preferences", "POST").status_code == 204
    assert (
        _auth(client, "/grafana/api/user/stars/dashboard/uid/abc", "POST").status_code
        == 204
    )


def test_current_grafana_bootstrap_and_namespaced_dashboard_permissions(gateway):
    client, _, member, integration, _, _ = gateway
    for path in (
        "/grafana/api/user/teams",
        "/grafana/api/user/stars",
        "/grafana/api/access-control/user/permissions",
        "/grafana/apis/dashboard.grafana.app/",
        "/grafana/apis/dashboard.grafana.app/v1beta1",
        "/grafana/apis/folder.grafana.app/v1beta1/",
        "/grafana/apis/dashboard.grafana.app/v1beta1/namespaces/org-7/dashboards",
    ):
        assert _auth(client, path).status_code == 204
    feature_query = "/grafana/apis/features.grafana.app/v0alpha1/namespaces/org-7/ofrep/v1/evaluate/flags"
    assert _auth(client, feature_query, "POST").status_code == 204
    dashboard = "/grafana/apis/dashboard.grafana.app/v1beta1/namespaces/org-7/dashboards/example"
    assert _auth(client, dashboard, "PUT").status_code == 403
    member.admin = True
    assert _auth(client, dashboard, "PUT").status_code == 403
    integration.membership_fingerprint = membership_fingerprint(
        integration.organization
    )
    assert _auth(client, dashboard, "PUT").status_code == 204


def test_member_removal_blocks_already_open_session_even_when_sync_failed(gateway):
    client, _, member, integration, _, _ = gateway
    assert _auth(client).status_code == 204
    db.session.delete(member)
    db.session.flush()
    integration.last_error = "membership API unavailable"
    assert _auth(client).status_code == 403


def test_role_downgrade_blocks_stale_editor_session_before_sync(gateway):
    client, _, member, integration, _, _ = gateway
    member.admin = True
    integration.membership_fingerprint = membership_fingerprint(
        integration.organization
    )
    assert _auth(client, "/grafana/api/dashboards/db", "POST").status_code == 204
    member.admin = False
    assert _auth(client, "/grafana/api/dashboards/db", "POST").status_code == 403


def test_cross_org_session_switch_is_denied(gateway):
    client, _, _, _, other_integration, _ = gateway
    with client.session_transaction() as session:
        session["grafana_organization_id"] = str(other_integration.organization_id)
    assert _auth(client).status_code == 403


def test_disabled_integration_blocks_auth(gateway):
    client, _, _, integration, _, _ = gateway
    assert _auth(client).status_code == 204
    integration.enabled = False
    assert _auth(client).status_code == 403


def test_auth_returns_401_when_unauthenticated(gateway):
    client, _, _, _, _, _ = gateway
    assert _auth(client).status_code == 204
    with client.session_transaction() as session:
        session.clear()
    # This fixture retains an app context; production creates a fresh g per request.
    g.pop("_login_user", None)
    assert _auth(client).status_code == 401


def test_tenants_requires_registry_key_header(gateway, monkeypatch):
    client, *_ = gateway
    assert _tenants(client).status_code == 403
    assert _tenants(client, key="wrong").status_code == 403
    assert _tenants(client, key="test-registry-key").status_code == 200
    monkeypatch.setitem(app.config, "MONITORING_TENANT_REGISTRY_KEY", None)
    assert _tenants(client, key="test-registry-key").status_code == 403


def test_mfa_guard_fails_closed_without_redirecting_auth_request(gateway, monkeypatch):
    client, _, _, _, _, _ = gateway
    monkeypatch.setitem(app.config, "REQUIRE_MFA", True)
    assert _auth(client).status_code == 403
    # The tenant registry endpoint works independently of browser MFA state.
    assert _tenants(client, key="test-registry-key").status_code == 200


def test_disable_immediately_revokes_browser_access_when_broker_is_down(gateway):
    client, _, member, integration, _, _ = gateway
    member.admin = True
    integration.membership_fingerprint = membership_fingerprint(
        integration.organization
    )
    with patch(
        "cabotage.celery.tasks.grafana.reconcile_organization.delay",
        side_effect=OperationalError("broker offline"),
    ):
        response = client.post(
            f"/organizations/{integration.organization.slug}/grafana/settings",
            data={"action": "disable"},
        )
    assert response.status_code == 302
    assert integration.enabled is False
    assert _auth(client).status_code == 403


def test_open_grafana_only_targets_this_orgs_projects_and_apps(gateway):
    client, _, _, integration, other_integration, environment = gateway
    project = environment.project
    other_project = Project(name="Other", organization=other_integration.organization)
    db.session.add(other_project)
    db.session.flush()
    own_app = Application(name="web", slug="web", project=project)
    other_app = Application(name="web", slug="web", project=other_project)
    db.session.add_all([own_app, other_app])
    db.session.flush()

    def opened(**data):
        return client.post(
            f"/organizations/{integration.organization.slug}/grafana/open", data=data
        ).headers["Location"]

    overview = "/grafana/d/cabotage-overview"
    assert (
        opened(application_id=own_app.id) == f"/grafana/d/{app_dashboard_uid(own_app)}"
    )
    assert opened(project_id=project.id) == (
        f"/grafana/dashboards/f/{project_folder_uid(project)}/"
    )
    assert opened(application_id=other_app.id) == overview
    assert opened(project_id=other_project.id) == overview
    assert opened(project_id="not-a-uuid") == overview


def test_registry_withdraws_deleted_workloads_and_ignores_disabled_ingresses(gateway):
    client, _, _, integration, _, environment = gateway
    application = Application(name="web", slug="web", project=environment.project)
    enrollment = ApplicationEnvironment(
        application=application, environment=environment
    )
    db.session.add_all([application, enrollment])
    db.session.flush()
    db.session.add_all(
        [
            Ingress(name="public", enabled=True, application_environment=enrollment),
            Ingress(name="private", enabled=False, application_environment=enrollment),
        ]
    )
    db.session.flush()

    def registry():
        response = _tenants(client, key="test-registry-key")
        assert response.status_code == 200
        return next(
            row
            for row in response.json["tenants"]
            if row["organization_id"] == str(integration.organization_id)
        )

    services = registry()["services"]
    assert {row["namespace"] for row in services} == {environment.k8s_namespace}
    assert all(
        "-public-" in row["name"] and "-private-" not in row["name"] for row in services
    )
    assert {row["application"] for row in services} == {"web"}
    enrollment.deleted_at = datetime.datetime(2000, 1, 1)
    db.session.flush()
    assert registry()["services"] == []


def test_traefik_edge_ignores_ingress_nginx_style_headers(gateway, monkeypatch):
    client, _, _, _, _, _ = gateway
    monkeypatch.setitem(app.config, "GRAFANA_EDGE", "traefik")
    # Only the ingress-nginx style headers are set; traefik mode must not
    # fall back to them.
    response = client.get(
        "/grafana-integration/auth",
        headers={
            "X-Original-URI": "/grafana/api/search",
            "X-Original-Method": "GET",
        },
    )
    assert response.status_code == 403
    response = client.get(
        "/grafana-integration/auth",
        headers={
            "X-Forwarded-Uri": "/grafana/api/search",
            "X-Forwarded-Method": "GET",
        },
    )
    assert response.status_code == 204


def test_ingress_nginx_edge_ignores_forged_traefik_style_headers(gateway):
    client, _, _, _, _, _ = gateway
    # Default edge is ingress-nginx. A forged X-Forwarded-Uri pointing at an
    # allowed path must not override a denied X-Original-URI.
    response = client.get(
        "/grafana-integration/auth",
        headers={
            "X-Original-URI": "/grafana/api/admin/users",
            "X-Original-Method": "POST",
            "X-Forwarded-Uri": "/grafana/api/search",
            "X-Forwarded-Method": "GET",
        },
    )
    assert response.status_code == 403


def test_invalid_grafana_edge_is_not_configured(gateway, monkeypatch):
    client, _, _, _, _, _ = gateway
    monkeypatch.setitem(app.config, "GRAFANA_EDGE", "haproxy")
    assert _auth(client).status_code == 403
    # The tenant registry router isn't behind the edge proxy, so its
    # authentication is independent of GRAFANA_EDGE.
    assert _tenants(client, key="test-registry-key").status_code == 200
