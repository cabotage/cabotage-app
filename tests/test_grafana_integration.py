"""Regression tests for org-scoped Grafana provisioning.

Covers config/URL validation and the reconcile loop's boundary, error, and
idempotence behavior against a small in-memory fake of the Grafana admin API
(not a mock-forwarding echo — the fake actually tracks org/user/datasource/
dashboard state so assertions exercise real reconcile logic).

Every test runs inside an outer DB transaction that is rolled back in
teardown (SAVEPOINT-backed, see ``db_session``), so the explicit
``db.session.commit()`` calls inside ``reconcile_grafana_organization``
never leave persistent rows behind.
"""

import datetime
import re
import uuid
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.integrations.grafana import (
    GrafanaIntegrationError,
    _is_valid_internal_url,
    _is_valid_same_origin_path,
    grafana_configured,
    membership_fingerprint,
    organization_tenants,
    reconcile_grafana_organization,
)
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.grafana import OrganizationGrafanaIntegration
from cabotage.server.models.projects import Environment, Project
from cabotage.server.wsgi import app as _app


@pytest.fixture
def app():
    original = {
        key: _app.config.get(key)
        for key in (
            "TESTING",
            "WTF_CSRF_ENABLED",
            "GRAFANA_ENABLED",
            "GRAFANA_EDGE",
            "GRAFANA_URL",
            "GRAFANA_PUBLIC_PATH",
            "GRAFANA_ADMIN_USERNAME",
            "GRAFANA_ADMIN_PASSWORD",
            "GRAFANA_DATA_SOURCE_CA_CERT_PATH",
            "GRAFANA_TIMEOUT",
            "GRAFANA_VERIFY",
            "MIMIR_URL",
            "LOKI_URL",
        )
    }
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["GRAFANA_ENABLED"] = True
    _app.config["GRAFANA_EDGE"] = "ingress-nginx"
    _app.config["GRAFANA_URL"] = "http://grafana-admin:3000"
    _app.config["GRAFANA_PUBLIC_PATH"] = "/grafana/"
    _app.config["GRAFANA_ADMIN_USERNAME"] = "admin"
    _app.config["GRAFANA_ADMIN_PASSWORD"] = "admin"  # nosec B105
    _app.config["GRAFANA_DATA_SOURCE_CA_CERT_PATH"] = None
    _app.config["GRAFANA_TIMEOUT"] = 5
    _app.config["GRAFANA_VERIFY"] = False
    _app.config["MIMIR_URL"] = "http://mimir:8080"
    _app.config["LOKI_URL"] = "http://loki:3100"
    with _app.app_context():
        yield _app
    _app.config.update(original)


@pytest.fixture
def db_session(app):
    """A session bound to a SAVEPOINT so nothing this test (or the code
    under test) commits survives past teardown.

    ``reconcile_grafana_organization`` calls ``db.session.commit()``
    directly as part of its normal operation; with
    ``join_transaction_mode="create_savepoint"`` those calls only
    release/reopen a SAVEPOINT nested inside the outer connection-level
    transaction, which this fixture always rolls back.
    """
    connection = db.engine.connect()
    outer_transaction = connection.begin()
    db.session.remove()
    db.session.configure(bind=connection, join_transaction_mode="create_savepoint")

    yield db.session

    db.session.remove()
    outer_transaction.rollback()
    connection.close()
    db.session.configure(bind=db.engine, join_transaction_mode="conditional_savepoint")


@pytest.fixture
def org(db_session):
    o = Organization(
        name="Grafana Test Org", slug=f"grafana-org-{uuid.uuid4().hex[:8]}"
    )
    db_session.add(o)
    db_session.flush()
    return o


@pytest.fixture
def other_org(db_session):
    o = Organization(
        name="Other Grafana Org", slug=f"grafana-other-{uuid.uuid4().hex[:8]}"
    )
    db_session.add(o)
    db_session.flush()
    return o


def _make_user(db_session, label, active=True):
    user = User(
        username=f"{label}-{uuid.uuid4().hex[:8]}",
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=active,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def member_user(db_session):
    return _make_user(db_session, "grafana-member")


@pytest.fixture
def admin_user(db_session):
    return _make_user(db_session, "grafana-admin")


@pytest.fixture
def integration(db_session, org):
    gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
    db_session.add(gi)
    db_session.flush()
    return gi


# ---------------------------------------------------------------------------
# Config / URL validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "http://admin:secret@grafana:3000",
            "http://:secret@grafana:3000",  # empty username still carries userinfo
            "http://grafana:3000/?token=abc",
            "http://grafana:3000/#frag",
            "ftp://grafana:3000",
            "grafana:3000",
            "http://grafana:999999",  # out-of-range port
            "http://grafana\x00:3000",  # control character
            "http://grafana:3000\\path",  # backslash
        ],
    )
    def test_rejects_invalid_internal_urls(self, app, value):
        assert not _is_valid_internal_url(value)

    @pytest.mark.parametrize(
        "value",
        [
            "http://grafana-admin:3000",
            "https://grafana.internal",
            "http://grafana-admin:3000/base",
        ],
    )
    def test_accepts_valid_internal_urls(self, app, value):
        assert _is_valid_internal_url(value)

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "https://evil.example/grafana/",  # not same-origin
            "grafana",  # not absolute
            "/grafana?x=1",  # query not allowed
            "/",  # bare root rejected
            "/grafana",  # missing trailing slash
            "//grafana/",  # parses as a network-path (netloc) reference
            "/grafana//sub/",  # empty segment
            "/grafana/../admin/",  # dot-dot segment
            "/grafana/./x/",  # dot segment
            "/grafana%2e%2e/",  # percent-encoding rejected outright
            "/grafana\\x/",  # backslash
            "/grafana/\x00/",  # control character
        ],
    )
    def test_rejects_invalid_same_origin_paths(self, app, value):
        assert not _is_valid_same_origin_path(value)

    def test_accepts_canonical_same_origin_path(self, app):
        assert _is_valid_same_origin_path("/grafana/")

    def test_grafana_configured_false_on_malformed_mimir_url(self, app):
        _app.config["MIMIR_URL"] = "http://user:pw@mimir:8080"
        assert grafana_configured() is False

    def test_grafana_configured_false_on_malformed_loki_url(self, app):
        _app.config["LOKI_URL"] = "http://user:pw@loki:3100"
        assert grafana_configured() is False

    def test_grafana_configured_false_on_malformed_public_path(self, app):
        _app.config["GRAFANA_PUBLIC_PATH"] = "http://evil.example/grafana/"
        assert grafana_configured() is False

    def test_grafana_configured_false_without_admin_credentials(self, app):
        _app.config["GRAFANA_ADMIN_USERNAME"] = None
        _app.config["GRAFANA_ADMIN_PASSWORD"] = None
        assert grafana_configured() is False

    @pytest.mark.parametrize("value", [None, "", "haproxy", "ingress-nginx "])
    def test_grafana_configured_false_on_invalid_edge(self, app, value):
        _app.config["GRAFANA_EDGE"] = value
        assert grafana_configured() is False

    def test_grafana_configured_true_with_traefik_edge(self, app):
        _app.config["GRAFANA_EDGE"] = "traefik"
        assert grafana_configured() is True

    def test_grafana_configured_true_with_full_config(self, app):
        assert grafana_configured() is True

    def test_grafana_configured_false_when_disabled(self, app):
        _app.config["GRAFANA_ENABLED"] = False
        assert grafana_configured() is False


# ---------------------------------------------------------------------------
# membership_fingerprint
# ---------------------------------------------------------------------------


class TestMembershipFingerprint:
    def test_stable_for_unchanged_state(self, app, db_session, org, member_user):
        org.add_user(member_user, admin=False)
        db_session.flush()
        assert membership_fingerprint(org) == membership_fingerprint(org)

    def test_changes_on_role_promotion(self, app, db_session, org, member_user):
        org.add_user(member_user, admin=False)
        db_session.flush()
        before = membership_fingerprint(org)

        assoc = OrganizationMember.query.filter_by(
            organization_id=org.id, user_id=member_user.id
        ).one()
        assoc.admin = True
        db_session.flush()
        db_session.expire(org, ["members"])

        assert membership_fingerprint(org) != before

    def test_changes_on_membership_removal(
        self, app, db_session, org, member_user, admin_user
    ):
        org.add_user(member_user, admin=False)
        org.add_user(admin_user, admin=True)
        db_session.flush()
        before = membership_fingerprint(org)

        org.remove_user(member_user)
        db_session.flush()
        db_session.expire(org, ["members"])

        assert membership_fingerprint(org) != before

    def test_excludes_inactive_members(self, app, db_session, org, member_user):
        inactive_user = _make_user(db_session, "grafana-inactive", active=False)
        org.add_user(member_user, admin=False)
        db_session.flush()
        without_inactive = membership_fingerprint(org)

        org.add_user(inactive_user, admin=False)
        db_session.flush()
        db_session.expire(org, ["members"])

        assert membership_fingerprint(org) == without_inactive

    def test_unchanged_when_tenant_set_changes(self, app, db_session, org, member_user):
        # A new environment must not close Grafana access; reconcile picks
        # the tenant up by rewriting the datasources.
        org.add_user(member_user, admin=False)
        db_session.flush()
        before = membership_fingerprint(org)

        project = Project(name="svc", organization_id=org.id, environments_enabled=True)
        db_session.add(project)
        db_session.flush()
        env = Environment(
            name="prod", project_id=project.id, uses_environment_namespace=True
        )
        db_session.add(env)
        db_session.flush()
        db_session.expire(org, ["projects"])

        assert membership_fingerprint(org) == before


# ---------------------------------------------------------------------------
# organization_tenants
# ---------------------------------------------------------------------------


class TestOrganizationTenants:
    def test_includes_org_and_active_environment_namespaces(self, app, db_session, org):
        project = Project(name="svc", organization_id=org.id, environments_enabled=True)
        db_session.add(project)
        db_session.flush()
        env = Environment(
            name="prod",
            project_id=project.id,
            uses_environment_namespace=True,
        )
        db_session.add(env)
        db_session.flush()

        tenants = organization_tenants(org)

        assert org.k8s_identifier in tenants
        assert env.k8s_namespace in tenants
        assert tenants == sorted(set(tenants))

    def test_excludes_deleted_projects_and_environments(self, app, db_session, org):
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)

        live_project = Project(name="live", organization_id=org.id)
        deleted_project = Project(name="gone", organization_id=org.id, deleted_at=now)
        db_session.add_all([live_project, deleted_project])
        db_session.flush()

        live_env = Environment(
            name="prod", project_id=live_project.id, uses_environment_namespace=True
        )
        deleted_env = Environment(
            name="old",
            project_id=live_project.id,
            uses_environment_namespace=True,
            deleted_at=now,
        )
        gone_env = Environment(
            name="prod",
            project_id=deleted_project.id,
            uses_environment_namespace=True,
        )
        db_session.add_all([live_env, deleted_env, gone_env])
        db_session.flush()

        tenants = organization_tenants(org)

        assert live_env.k8s_namespace in tenants
        assert deleted_env.k8s_namespace not in tenants
        assert gone_env.k8s_namespace not in tenants

    def test_never_includes_shared_infra_or_legacy_tenants(self, app, db_session, org):
        tenants = set(organization_tenants(org))
        assert "cabotage-infra" not in tenants
        assert "anonymous" not in tenants
        assert "fake" not in tenants


# ---------------------------------------------------------------------------
# reconcile_grafana_organization against a fake Grafana admin API
# ---------------------------------------------------------------------------


class _FakeGrafana:
    """Minimal, stateful stand-in for the Grafana admin HTTP API.

    Tracks orgs/users/memberships/datasources/dashboards so tests can
    assert on real convergence behavior (no duplicates, correct role
    assignment, stale removal, preserved customer edits) rather than
    merely that a call was made.
    """

    def __init__(self):
        self.next_org_id = 100
        self.next_user_id = 200
        self.orgs: dict[int, str] = {}
        self.org_users: dict[int, dict[str, dict]] = {}
        self.users: dict[str, int] = {}
        self.datasources: dict[tuple[int, str], dict] = {}
        self.dashboards: dict[tuple[int, str], dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_on: set[tuple[str, str]] = set()
        self.settings: dict[str, dict[str, str]] = {
            "users": {
                "auto_assign_org": "true",
                "auto_assign_org_role": "Viewer",
                "viewers_can_edit": "false",
                "editors_can_admin": "false",
            },
            "auth.anonymous": {"enabled": "false"},
        }

    def __call__(
        self,
        method,
        url,
        auth=None,
        headers=None,
        verify=None,
        timeout=None,
        json=None,
        params=None,
        **kwargs,
    ):
        path = urlparse(url).path
        self.calls.append((method, path))
        if (method, path) in self.fail_on:
            resp = MagicMock()
            resp.status_code = 500
            resp.json.return_value = {"message": "boom"}
            return resp

        org_header = (headers or {}).get("X-Grafana-Org-Id")
        org_id = int(org_header) if org_header is not None else None

        resp = MagicMock()

        if method == "GET" and path == "/api/admin/settings":
            resp.status_code = 200
            resp.json.return_value = self.settings
            return resp

        m = re.fullmatch(r"/api/orgs/(\d+)", path)
        if method == "GET" and m:
            oid = int(m.group(1))
            if oid in self.orgs:
                resp.status_code = 200
                resp.json.return_value = {"id": oid, "name": self.orgs[oid]}
            else:
                resp.status_code = 404
            return resp

        m = re.fullmatch(r"/api/orgs/name/([^/]+)", path)
        if method == "GET" and m:
            name = unquote(m.group(1))
            for oid, existing_name in self.orgs.items():
                if existing_name == name:
                    resp.status_code = 200
                    resp.json.return_value = {"id": oid, "name": existing_name}
                    return resp
            resp.status_code = 404
            return resp

        if method == "POST" and path == "/api/orgs":
            name = json["name"]
            oid = self.next_org_id
            self.next_org_id += 1
            self.orgs[oid] = name
            self.org_users[oid] = {
                "admin": {"userId": 1, "login": "admin", "role": "Admin"}
            }
            resp.status_code = 200
            resp.json.return_value = {"orgId": oid}
            return resp

        if method == "GET" and path == "/api/users/lookup":
            login = (
                params["loginOrEmail"]
                if params
                else parse_qs(urlparse(url).query)["loginOrEmail"][0]
            )
            if login in self.users:
                resp.status_code = 200
                resp.json.return_value = {"id": self.users[login]}
            else:
                resp.status_code = 404
            return resp

        if method == "POST" and path == "/api/admin/users":
            # Grafana's real admin-create-user API auto-assigns new users
            # per [users] auto_assign_org settings when OrgId is absent —
            # in production that creates a personal org where they land as
            # Admin. The app must always pin OrgId explicitly; model that
            # requirement here so a regression fails loudly instead of
            # silently matching looser fake behavior.
            assert "OrgId" in json, "user creation must pin OrgId"
            login = json["login"]
            uid = self.next_user_id
            self.next_user_id += 1
            self.users[login] = uid
            oid = json["OrgId"]
            self.org_users.setdefault(oid, {})[login] = {
                "userId": uid,
                "login": login,
                "role": "Viewer",
            }
            resp.status_code = 200
            resp.json.return_value = {"id": uid}
            return resp

        m = re.fullmatch(r"/api/orgs/(\d+)/users", path)
        if m and method == "GET":
            oid = int(m.group(1))
            resp.status_code = 200
            resp.json.return_value = list(self.org_users.get(oid, {}).values())
            return resp
        if m and method == "POST":
            oid = int(m.group(1))
            login = json["loginOrEmail"]
            role = json["role"]
            uid = self.users.get(login)
            if uid is None:
                resp.status_code = 404
                return resp
            if login in self.org_users.get(oid, {}):
                # Grafana rejects adding a user who is already a member of
                # the organization; the OrgId-pinned creation above already
                # makes this true for every user this app ever creates.
                resp.status_code = 500
                resp.json.return_value = {"message": "user is already a member"}
                return resp
            self.org_users.setdefault(oid, {})[login] = {
                "userId": uid,
                "login": login,
                "role": role,
            }
            resp.status_code = 200
            resp.json.return_value = {}
            return resp

        m = re.fullmatch(r"/api/orgs/(\d+)/users/(\d+)", path)
        if m:
            oid, uid = int(m.group(1)), int(m.group(2))
            entries = self.org_users.get(oid, {})
            if method == "PATCH":
                for entry in entries.values():
                    if entry["userId"] == uid:
                        entry["role"] = json["role"]
                        resp.status_code = 200
                        resp.json.return_value = {}
                        return resp
                resp.status_code = 404
                return resp
            if method == "DELETE":
                for login, entry in list(entries.items()):
                    if entry["userId"] == uid:
                        if (
                            entry["role"] == "Admin"
                            and sum(
                                member["role"] == "Admin" for member in entries.values()
                            )
                            == 1
                        ):
                            resp.status_code = 400
                            return resp
                        del entries[login]
                        resp.status_code = 200
                        resp.json.return_value = {}
                        return resp
                resp.status_code = 404
                return resp

        m = re.fullmatch(r"/api/datasources/uid/([^/]+)", path)
        if m:
            key = (org_id, m.group(1))
            if method == "GET":
                if key in self.datasources:
                    resp.status_code = 200
                    resp.json.return_value = self.datasources[key]
                else:
                    resp.status_code = 404
                return resp
            if method == "PUT":
                self.datasources[key] = json
                resp.status_code = 200
                resp.json.return_value = json
                return resp

        if method == "POST" and path == "/api/datasources":
            key = (org_id, json["uid"])
            self.datasources[key] = json
            resp.status_code = 200
            resp.json.return_value = json
            return resp

        m = re.fullmatch(r"/api/dashboards/uid/([^/]+)", path)
        if method == "GET" and m:
            key = (org_id, m.group(1))
            if key in self.dashboards:
                resp.status_code = 200
                resp.json.return_value = {"dashboard": self.dashboards[key]}
            else:
                resp.status_code = 404
            return resp

        if method == "POST" and path == "/api/dashboards/db":
            dashboard = json["dashboard"]
            key = (org_id, dashboard["uid"])
            self.dashboards[key] = dashboard
            resp.status_code = 200
            resp.json.return_value = {"uid": dashboard["uid"]}
            return resp

        raise AssertionError(f"unhandled fake Grafana request: {method} {path}")


@pytest.fixture
def fake_grafana():
    fake = _FakeGrafana()
    with patch(
        "cabotage.server.integrations.grafana.requests.request", side_effect=fake
    ):
        yield fake


class TestReconcileGrafanaOrganization:
    def test_provisions_org_users_datasources_and_dashboard(
        self, app, db_session, org, member_user, admin_user, fake_grafana
    ):
        org.add_user(member_user, admin=False)
        org.add_user(admin_user, admin=True)
        db_session.flush()

        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)

        db_session.refresh(gi)
        assert gi.grafana_org_id is not None
        assert gi.membership_fingerprint == membership_fingerprint(org)
        assert gi.last_error is None
        assert gi.ready

        # Stable naming keyed on the immutable org UUID, not the slug.
        assert fake_grafana.orgs[gi.grafana_org_id] == f"cabotage-{org.id}"

        org_users = fake_grafana.org_users[gi.grafana_org_id]
        assert org_users[str(member_user.id)]["role"] == "Viewer"
        assert org_users[str(admin_user.id)]["role"] == "Editor"
        # Never assign the Grafana Admin / server-admin role to a customer.
        assert org_users["admin"]["role"] == "Admin"
        assert all(
            entry["role"] != "Admin"
            for login, entry in org_users.items()
            if login != "admin"
        )

        assert (gi.grafana_org_id, "cabotage-prometheus") in fake_grafana.datasources
        assert (gi.grafana_org_id, "cabotage-loki") in fake_grafana.datasources
        assert (gi.grafana_org_id, "cabotage-overview") in fake_grafana.dashboards

    def test_datasource_payload_urls_and_tenant_scope_header(
        self, app, db_session, org, member_user, fake_grafana
    ):
        org.add_user(member_user, admin=False)
        db_session.flush()
        project = Project(name="svc", organization_id=org.id, environments_enabled=True)
        db_session.add(project)
        db_session.flush()
        env = Environment(
            name="prod", project_id=project.id, uses_environment_namespace=True
        )
        db_session.add(env)
        db_session.flush()

        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)

        prometheus = fake_grafana.datasources[
            (gi.grafana_org_id, "cabotage-prometheus")
        ]
        loki = fake_grafana.datasources[(gi.grafana_org_id, "cabotage-loki")]
        assert prometheus["url"] == "http://mimir:8080/prometheus"
        assert loki["url"] == "http://loki:3100"
        assert prometheus["editable"] is False
        assert loki["editable"] is False
        for payload in (prometheus, loki):
            assert payload["jsonData"]["httpHeaderName1"] == "X-Scope-OrgID"
            tenant_value = payload["secureJsonData"]["httpHeaderValue1"]
            scoped_tenants = set(tenant_value.split("|"))
            assert scoped_tenants == set(organization_tenants(org))
            assert "cabotage-infra" not in scoped_tenants
            assert "anonymous" not in scoped_tenants
            assert "fake" not in scoped_tenants

    def test_tenant_set_change_updates_datasources_on_reconcile(
        self, app, db_session, org, member_user, fake_grafana
    ):
        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)
        before = fake_grafana.datasources[(gi.grafana_org_id, "cabotage-prometheus")][
            "secureJsonData"
        ]["httpHeaderValue1"]

        project = Project(name="svc", organization_id=org.id, environments_enabled=True)
        db_session.add(project)
        db_session.flush()
        env = Environment(
            name="prod", project_id=project.id, uses_environment_namespace=True
        )
        db_session.add(env)
        db_session.flush()
        db_session.expire(org, ["projects"])

        reconcile_grafana_organization(org.id)

        after = fake_grafana.datasources[(gi.grafana_org_id, "cabotage-prometheus")][
            "secureJsonData"
        ]["httpHeaderValue1"]
        assert after != before
        assert env.k8s_namespace in after.split("|")

    def test_datasources_trust_configured_ca_cert(
        self, app, db_session, org, member_user, fake_grafana, tmp_path
    ):
        ca_cert_path = tmp_path / "ca.crt"
        ca_cert_path.write_text(
            "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"
        )
        _app.config["GRAFANA_DATA_SOURCE_CA_CERT_PATH"] = str(ca_cert_path)

        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)

        for uid in ("cabotage-prometheus", "cabotage-loki"):
            payload = fake_grafana.datasources[(gi.grafana_org_id, uid)]
            assert payload["jsonData"]["tlsAuthWithCACert"] is True
            assert payload["secureJsonData"]["tlsCACert"] == ca_cert_path.read_text()

    def test_datasources_omit_ca_cert_when_not_configured(
        self, app, db_session, org, member_user, fake_grafana
    ):
        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)

        payload = fake_grafana.datasources[(gi.grafana_org_id, "cabotage-prometheus")]
        assert "tlsAuthWithCACert" not in payload["jsonData"]
        assert "tlsCACert" not in payload["secureJsonData"]

    def test_missing_ca_cert_file_raises_sanitized_error(
        self, app, db_session, org, member_user, fake_grafana, tmp_path
    ):
        _app.config["GRAFANA_DATA_SOURCE_CA_CERT_PATH"] = str(tmp_path / "missing.crt")

        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        # Commit so this setup survives the SAVEPOINT rollback triggered by
        # the reconcile failure below.
        db_session.commit()

        with pytest.raises(GrafanaIntegrationError):
            reconcile_grafana_organization(org.id)

        db_session.refresh(gi)
        assert gi.last_error is not None
        assert str(tmp_path) not in gi.last_error
        assert not gi.ready

    def test_reconcile_twice_is_idempotent_no_duplicates(
        self, app, db_session, org, member_user, fake_grafana
    ):
        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)
        first_org_id = gi.grafana_org_id

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)

        assert gi.grafana_org_id == first_org_id
        assert len(fake_grafana.orgs) == 1
        assert len(fake_grafana.datasources) == 2
        assert set(fake_grafana.dashboards) == {
            (first_org_id, "cabotage-overview"),
            (first_org_id, "cabotage-logs"),
        }
        org_create_calls = [c for c in fake_grafana.calls if c == ("POST", "/api/orgs")]
        assert len(org_create_calls) == 1
        datasource_create_calls = [
            c for c in fake_grafana.calls if c == ("POST", "/api/datasources")
        ]
        assert len(datasource_create_calls) == 2
        dashboard_create_calls = [
            c for c in fake_grafana.calls if c == ("POST", "/api/dashboards/db")
        ]
        assert len(dashboard_create_calls) == 2

    def test_stable_org_name_survives_rename_and_lost_local_id(
        self, app, db_session, org, member_user, fake_grafana
    ):
        """Renaming the org (or losing track of grafana_org_id) must not
        orphan or duplicate the Grafana org: lookup is keyed on the
        immutable organization UUID, not the mutable slug."""
        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)
        first_org_id = gi.grafana_org_id

        org.slug = f"renamed-{uuid.uuid4().hex[:8]}"
        gi.grafana_org_id = None
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)

        assert gi.grafana_org_id == first_org_id
        assert len(fake_grafana.orgs) == 1

    def test_partial_provisioning_retry_reuses_org(
        self, app, db_session, org, member_user, fake_grafana
    ):
        """If membership reconciliation fails after the org itself was
        created, the org id must already be persisted so a retry reuses it
        instead of creating (or orphaning) a duplicate."""
        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        # Commit (not just flush) so this setup survives the SAVEPOINT
        # rollback triggered by the reconcile failure below — mirrors how
        # the real settings handler commits the integration row before the
        # Celery task ever runs.
        db_session.commit()

        with patch(
            "cabotage.server.integrations.grafana.requests.request",
            side_effect=_FailFirstMembershipList(fake_grafana),
        ):
            with pytest.raises(GrafanaIntegrationError):
                reconcile_grafana_organization(org.id)

        db_session.refresh(gi)
        assert gi.grafana_org_id is not None
        persisted_org_id = gi.grafana_org_id
        assert len(fake_grafana.orgs) == 1

        with patch(
            "cabotage.server.integrations.grafana.requests.request",
            side_effect=fake_grafana,
        ):
            reconcile_grafana_organization(org.id)

        db_session.refresh(gi)
        assert gi.grafana_org_id == persisted_org_id
        assert len(fake_grafana.orgs) == 1
        assert gi.ready

    def test_removes_stale_membership(
        self, app, db_session, org, member_user, admin_user, fake_grafana
    ):
        org.add_user(member_user, admin=False)
        org.add_user(admin_user, admin=True)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)

        org.remove_user(member_user)
        db_session.flush()
        db_session.expire(org, ["members"])

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)

        org_users = fake_grafana.org_users[gi.grafana_org_id]
        assert str(member_user.id) not in org_users
        assert str(admin_user.id) in org_users

    def test_deactivated_member_is_revoked(
        self, app, db_session, org, member_user, fake_grafana
    ):
        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)
        assert str(member_user.id) in fake_grafana.org_users[gi.grafana_org_id]

        member_user.active = False
        db_session.flush()
        db_session.expire(org, ["members"])

        reconcile_grafana_organization(org.id)

        assert str(member_user.id) not in fake_grafana.org_users[gi.grafana_org_id]

    def test_role_change_updates_existing_membership(
        self, app, db_session, org, member_user, fake_grafana
    ):
        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)
        assert (
            fake_grafana.org_users[gi.grafana_org_id][str(member_user.id)]["role"]
            == "Viewer"
        )

        assoc = OrganizationMember.query.filter_by(
            organization_id=org.id, user_id=member_user.id
        ).one()
        assoc.admin = True
        db_session.flush()
        db_session.expire(org, ["members"])

        reconcile_grafana_organization(org.id)

        assert (
            fake_grafana.org_users[gi.grafana_org_id][str(member_user.id)]["role"]
            == "Editor"
        )

    def test_preserves_customer_edited_starter_dashboard(
        self, app, db_session, org, member_user, fake_grafana
    ):
        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)

        keys = [
            (gi.grafana_org_id, uid) for uid in ("cabotage-overview", "cabotage-logs")
        ]
        for key in keys:
            fake_grafana.dashboards[key]["title"] = f"Customer Edited {key[1]}"
            fake_grafana.dashboards[key]["panels"] = []

        reconcile_grafana_organization(org.id)

        for key in keys:
            assert fake_grafana.dashboards[key]["title"] == f"Customer Edited {key[1]}"
            assert fake_grafana.dashboards[key]["panels"] == []
        dashboard_create_calls = [
            c for c in fake_grafana.calls if c == ("POST", "/api/dashboards/db")
        ]
        assert len(dashboard_create_calls) == 2

    def test_disabled_integration_revokes_access_but_preserves_org(
        self, app, db_session, org, member_user, fake_grafana
    ):
        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)
        grafana_org_id = gi.grafana_org_id
        assert fake_grafana.org_users[grafana_org_id]

        gi.enabled = False
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)

        assert gi.grafana_org_id == grafana_org_id
        assert grafana_org_id in fake_grafana.orgs  # org itself preserved
        assert fake_grafana.org_users[grafana_org_id] == {
            "admin": {"userId": 1, "login": "admin", "role": "Admin"}
        }
        assert gi.membership_fingerprint is None
        assert not gi.ready
        # Dashboards/datasources are left alone when disabling.
        assert (grafana_org_id, "cabotage-overview") in fake_grafana.dashboards

    def test_deleted_organization_revokes_access(
        self, app, db_session, org, member_user, fake_grafana
    ):
        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)
        grafana_org_id = gi.grafana_org_id

        org.deleted_at = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        db_session.flush()

        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)

        assert fake_grafana.org_users[grafana_org_id] == {
            "admin": {"userId": 1, "login": "admin", "role": "Admin"}
        }
        assert gi.membership_fingerprint is None

    def test_missing_integration_is_a_noop(self, app, db_session, org, fake_grafana):
        reconcile_grafana_organization(org.id)
        assert fake_grafana.calls == []

    def test_disabled_feature_is_a_noop(self, app, db_session, org, fake_grafana):
        _app.config["GRAFANA_ENABLED"] = False
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        reconcile_grafana_organization(org.id)

        assert fake_grafana.calls == []

    def test_failure_never_marks_ready_and_records_sanitized_error(
        self, app, db_session, org, member_user, fake_grafana
    ):
        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        # Commit so this setup survives the SAVEPOINT rollback triggered by
        # the reconcile failure below.
        db_session.commit()

        fake_grafana.fail_on.add(("GET", f"/api/orgs/name/cabotage-{org.id}"))

        with pytest.raises(GrafanaIntegrationError):
            reconcile_grafana_organization(org.id)

        db_session.refresh(gi)
        assert gi.membership_fingerprint is None
        assert not gi.ready
        assert gi.last_error is not None
        # Sanitized: no raw response body, auth header, or admin password leaks.
        assert "admin" not in gi.last_error.lower()
        assert "boom" not in gi.last_error

    @pytest.mark.parametrize(
        "section,key,value",
        [
            ("users", "auto_assign_org", "false"),
            ("users", "auto_assign_org_role", "Admin"),
            ("auth.anonymous", "enabled", "true"),
        ],
    )
    def test_unsafe_grafana_settings_block_provisioning(
        self, app, db_session, org, member_user, fake_grafana, section, key, value
    ):
        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.commit()
        fake_grafana.settings[section][key] = value

        with pytest.raises(GrafanaIntegrationError):
            reconcile_grafana_organization(org.id)

        db_session.refresh(gi)
        assert not gi.ready
        assert ("POST", "/api/admin/users") not in fake_grafana.calls
        assert ("POST", "/api/orgs") not in fake_grafana.calls

    def test_membership_api_failure_does_not_fail_open(
        self, app, db_session, org, member_user, fake_grafana
    ):
        org.add_user(member_user, admin=False)
        db_session.flush()
        gi = OrganizationGrafanaIntegration(organization_id=org.id, enabled=True)
        db_session.add(gi)
        db_session.flush()

        # First reconcile succeeds and creates the org.
        reconcile_grafana_organization(org.id)
        db_session.refresh(gi)
        grafana_org_id = gi.grafana_org_id
        ready_fingerprint = gi.membership_fingerprint

        # Add a second member, then make the membership list call fail.
        second_user = _make_user(db_session, "grafana-second")
        org.add_user(second_user, admin=False)
        db_session.flush()
        db_session.expire(org, ["members"])

        fake_grafana.fail_on.add(("GET", f"/api/orgs/{grafana_org_id}/users"))

        with pytest.raises(GrafanaIntegrationError):
            reconcile_grafana_organization(org.id)

        db_session.refresh(gi)
        # Must not silently keep the stale "ready" fingerprint from before
        # the failed attempt — a membership API failure cannot fail open.
        assert gi.membership_fingerprint is None
        assert not gi.ready
        assert gi.membership_fingerprint != ready_fingerprint


class _FailFirstMembershipList:
    """Delegates to a real ``_FakeGrafana`` but returns a 500 for the first
    org-users GET call only, so the retry path can be exercised against the
    same underlying state."""

    _ORG_USERS_RE = re.compile(r"/api/orgs/\d+/users$")

    def __init__(self, fake):
        self._fake = fake
        self._failed_once = False

    def __call__(self, method, url, **kwargs):
        path = urlparse(url).path
        if (
            not self._failed_once
            and method == "GET"
            and self._ORG_USERS_RE.fullmatch(path)
        ):
            self._failed_once = True
            resp = MagicMock()
            resp.status_code = 500
            resp.json.return_value = {"message": "boom"}
            return resp
        return self._fake(method, url, **kwargs)
