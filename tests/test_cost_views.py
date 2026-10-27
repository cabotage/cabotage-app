import time
import uuid
from unittest.mock import patch

import pytest
from flask_security import hash_password
from kombu.exceptions import OperationalError

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Project,
)
from cabotage.server.wsgi import app as _app


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REQUIRE_MFA"] = False
    _app.config["SHOW_COST_ESTIMATES"] = True
    with _app.app_context():
        yield _app
    _app.config["REQUIRE_MFA"] = True
    _app.config["SHOW_COST_ESTIMATES"] = False


@pytest.fixture
def client(app):
    return app.test_client()


def _make_user(prefix):
    user = User(
        username=f"{prefix}-{uuid.uuid4().hex[:8]}",
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def member_user(app):
    return _make_user("cost-member")


@pytest.fixture
def outsider_user(app):
    return _make_user("cost-outsider")


@pytest.fixture
def admin_user(app):
    user = _make_user("cost-admin")
    user.admin = True
    db.session.commit()
    return user


@pytest.fixture
def cost_context(member_user):
    org = Organization(name="Cost Org", slug=f"cost-org-{uuid.uuid4().hex[:8]}")
    db.session.add(org)
    db.session.flush()
    db.session.add(
        OrganizationMember(organization_id=org.id, user_id=member_user.id, admin=True)
    )

    project = Project(name="Cost Project", organization_id=org.id)
    db.session.add(project)
    db.session.flush()

    environment = Environment(name="production", project_id=project.id)
    db.session.add(environment)
    db.session.flush()

    application = Application(
        name="Cost App",
        slug=f"cost-app-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
    )
    db.session.add(application)
    db.session.commit()

    return {
        "org": org,
        "project": project,
        "environment": environment,
        "application": application,
    }


def _fake_report():
    return {
        "range": {
            "key": "mtd",
            "label": "Month to date",
            "start": "2026-10-01T00:00:00Z",
            "end": "2026-10-11T00:00:00Z",
            "interval": "day",
        },
        "totals": {"cost": 12.5, "previous_cost": 10.0, "change_pct": 25.0},
        "kpis": {
            "month_to_date": 12.5,
            "projected_month": 40.0,
            "run_rate_monthly": 38.0,
            "efficiency": 0.62,
        },
        "categories": [
            {"key": "compute", "label": "Compute", "cost": 8.0, "share": 0.64},
            {
                "key": "backing_service",
                "label": "Backing services",
                "cost": 2.0,
                "share": 0.16,
            },
            {"key": "storage", "label": "Storage", "cost": 1.0, "share": 0.08},
            {"key": "egress", "label": "Egress", "cost": 0.5, "share": 0.04},
            {"key": "build", "label": "Build", "cost": 0.5, "share": 0.04},
            {"key": "overhead", "label": "Overhead", "cost": 0.5, "share": 0.04},
        ],
        "meters": [
            {
                "key": "vcpu",
                "label": "vCPU",
                "unit": "vCPU-hr",
                "quantity": 100.0,
                "rate": 0.03488,
                "cost": 3.488,
            },
        ],
        "series": {"timestamps": [1760140800], "categories": {"compute": [8.0]}},
        "breakdowns": {
            "projects": [
                {
                    "key": "p1",
                    "name": "Cost Project",
                    "context": None,
                    "cost": 12.5,
                    "previous_cost": 10.0,
                    "share": 1.0,
                    "categories": {"compute": 8.0},
                    "efficiency": 0.6,
                    "slugs": {"project": "cost-project"},
                    "deleted": False,
                }
            ],
            "applications": [
                {
                    "key": "a1",
                    "name": "Cost App",
                    "context": "Cost Project",
                    "cost": 5.0,
                    "previous_cost": 4.0,
                    "share": 0.4,
                    "categories": {"compute": 5.0},
                    "efficiency": 0.5,
                    "slugs": {"project": "cost-project", "application": "cost-app"},
                    "deleted": False,
                }
            ],
        },
        "freshness": {
            "collection_enabled": True,
            "last_run_at": "2026-10-11T14:00:00Z",
            "data_through": "2026-10-11T14:00:00Z",
            "stale": False,
            "missing_hours": 0,
            "failed_hours": [],
        },
        "rates": {"vcpu_hour": 0.03488, "memory_gib_hour": 0.0025},
    }


class TestCostPermissions:
    def test_organization_cost_data_requires_membership(
        self, client, outsider_user, cost_context
    ):
        _login(client, outsider_user)
        org = cost_context["org"]
        resp = client.get(f"/organizations/{org.slug}/cost/data")
        assert resp.status_code == 403

    def test_cost_routes_404_when_disabled(
        self, app, client, member_user, cost_context
    ):
        app.config["SHOW_COST_ESTIMATES"] = False
        _login(client, member_user)
        org = cost_context["org"]
        assert client.get(f"/organizations/{org.slug}/cost").status_code == 404
        assert client.get(f"/organizations/{org.slug}/cost/data").status_code == 404

    def test_organization_cost_data_member_gets_report(
        self, client, member_user, cost_context
    ):
        _login(client, member_user)
        org = cost_context["org"]
        with patch(
            "cabotage.server.cost.views.build_report", return_value=_fake_report()
        ):
            resp = client.get(f"/organizations/{org.slug}/cost/data")
        assert resp.status_code == 200
        data = resp.get_json()
        assert (
            data["breakdowns"]["projects"][0]["url"]
            == f"/projects/{org.slug}/cost-project/cost"
        )
        assert (
            data["breakdowns"]["applications"][0]["url"]
            == f"/projects/{org.slug}/cost-project/applications/cost-app/cost"
        )

    def test_deleted_organization_returns_404(self, client, member_user, cost_context):
        _login(client, member_user)
        org = cost_context["org"]
        org.deleted_at = db.func.now()
        db.session.commit()
        resp = client.get(f"/organizations/{org.slug}/cost/data")
        assert resp.status_code == 404

    def test_project_cost_data_requires_membership(
        self, client, outsider_user, cost_context
    ):
        _login(client, outsider_user)
        org = cost_context["org"]
        project = cost_context["project"]
        resp = client.get(f"/projects/{org.slug}/{project.slug}/cost/data")
        assert resp.status_code == 403

    def test_project_cost_data_member_gets_report(
        self, client, member_user, cost_context
    ):
        _login(client, member_user)
        org = cost_context["org"]
        project = cost_context["project"]
        with patch(
            "cabotage.server.cost.views.build_report", return_value=_fake_report()
        ):
            resp = client.get(f"/projects/{org.slug}/{project.slug}/cost/data")
        assert resp.status_code == 200
        assert "totals" in resp.get_json()

    def test_environment_cost_data_requires_membership(
        self, client, outsider_user, cost_context
    ):
        _login(client, outsider_user)
        org = cost_context["org"]
        project = cost_context["project"]
        environment = cost_context["environment"]
        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/cost/data"
        )
        assert resp.status_code == 403

    def test_environment_cost_data_member_gets_report(
        self, client, member_user, cost_context
    ):
        _login(client, member_user)
        org = cost_context["org"]
        project = cost_context["project"]
        environment = cost_context["environment"]
        with patch(
            "cabotage.server.cost.views.build_report", return_value=_fake_report()
        ):
            resp = client.get(
                f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/cost/data"
            )
        assert resp.status_code == 200
        assert "totals" in resp.get_json()

    def test_application_cost_data_requires_membership(
        self, client, outsider_user, cost_context
    ):
        _login(client, outsider_user)
        org = cost_context["org"]
        project = cost_context["project"]
        application = cost_context["application"]
        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/cost/data"
        )
        assert resp.status_code == 403

    def test_application_cost_data_member_gets_report(
        self, client, member_user, cost_context
    ):
        _login(client, member_user)
        org = cost_context["org"]
        project = cost_context["project"]
        application = cost_context["application"]
        with patch(
            "cabotage.server.cost.views.build_report", return_value=_fake_report()
        ):
            resp = client.get(
                f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/cost/data"
            )
        assert resp.status_code == 200
        assert "totals" in resp.get_json()

    def test_deleted_application_returns_404(self, client, member_user, cost_context):
        _login(client, member_user)
        org = cost_context["org"]
        project = cost_context["project"]
        application = cost_context["application"]
        application.deleted_at = db.func.now()
        db.session.commit()
        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/cost/data"
        )
        assert resp.status_code == 404

    def test_platform_cost_data_requires_admin(self, client, member_user):
        _login(client, member_user)
        resp = client.get("/infra/cost/data")
        assert resp.status_code == 403

    def test_platform_cost_requires_admin(self, client, member_user):
        _login(client, member_user)
        resp = client.get("/infra/cost")
        assert resp.status_code == 403

    def test_platform_cost_data_admin_gets_report(self, client, admin_user):
        _login(client, admin_user)
        with patch(
            "cabotage.server.cost.views.build_report", return_value=_fake_report()
        ):
            resp = client.get("/infra/cost/data")
        assert resp.status_code == 200
        assert "totals" in resp.get_json()

    def test_platform_cost_admin_gets_page(self, client, admin_user):
        _login(client, admin_user)
        with patch(
            "cabotage.server.cost.views.build_report", return_value=_fake_report()
        ):
            resp = client.get("/infra/cost")
        assert resp.status_code == 200
        assert b"cost-dashboard" in resp.data


class TestCostCollection:
    def test_collection_requires_login(self, client):
        with patch("cabotage.server.cost.views.collect_costs.delay") as queue:
            resp = client.post("/infra/cost/collect")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
        queue.assert_not_called()

    def test_collection_requires_platform_admin(self, client, member_user):
        _login(client, member_user)
        with patch("cabotage.server.cost.views.collect_costs.delay") as queue:
            resp = client.post("/infra/cost/collect")
        assert resp.status_code == 403
        queue.assert_not_called()

    def test_collection_is_disabled_with_costs(
        self, app, client, admin_user, monkeypatch
    ):
        _login(client, admin_user)
        monkeypatch.setitem(app.config, "SHOW_COST_ESTIMATES", False)
        with patch("cabotage.server.cost.views.collect_costs.delay") as queue:
            resp = client.post("/infra/cost/collect")
        assert resp.status_code == 404
        queue.assert_not_called()

    def test_collection_requires_csrf(self, app, client, admin_user, monkeypatch):
        _login(client, admin_user)
        monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
        with patch("cabotage.server.cost.views.collect_costs.delay") as queue:
            resp = client.post("/infra/cost/collect")
        assert resp.status_code == 400
        queue.assert_not_called()

    def test_queue_failure_is_reported_without_success(self, client, admin_user):
        _login(client, admin_user)
        with patch(
            "cabotage.server.cost.views.collect_costs.delay",
            side_effect=OperationalError("broker unavailable"),
        ):
            resp = client.post("/infra/cost/collect?range=7d")
        assert resp.status_code == 303
        assert resp.headers["Location"] == "/infra/cost?range=7d"
        with client.session_transaction() as session:
            assert [category for category, _ in session["_flashes"]] == ["error"]


class TestCostRangeValidation:
    def test_invalid_range_falls_back_to_default(
        self, client, member_user, cost_context
    ):
        _login(client, member_user)
        org = cost_context["org"]
        with patch(
            "cabotage.server.cost.views.build_report", return_value=_fake_report()
        ) as mock_build:
            resp = client.get(f"/organizations/{org.slug}/cost/data?range=not-a-range")
        assert resp.status_code == 200
        called_range = mock_build.call_args[0][1]
        assert called_range == "mtd"


class TestCostPages:
    def test_organization_cost_page_renders(self, client, member_user, cost_context):
        _login(client, member_user)
        org = cost_context["org"]
        resp = client.get(f"/organizations/{org.slug}/cost")
        assert resp.status_code == 200
        assert b"cost-dashboard" in resp.data

    def test_project_cost_page_renders(self, client, member_user, cost_context):
        _login(client, member_user)
        org = cost_context["org"]
        project = cost_context["project"]
        resp = client.get(f"/projects/{org.slug}/{project.slug}/cost")
        assert resp.status_code == 200
        assert b"cost-dashboard" in resp.data

    def test_environment_cost_page_renders(self, client, member_user, cost_context):
        _login(client, member_user)
        org = cost_context["org"]
        project = cost_context["project"]
        environment = cost_context["environment"]
        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/environments/{environment.slug}/cost"
        )
        assert resp.status_code == 200
        assert b"cost-dashboard" in resp.data

    def test_application_cost_page_renders(self, client, member_user, cost_context):
        _login(client, member_user)
        org = cost_context["org"]
        project = cost_context["project"]
        application = cost_context["application"]
        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/cost"
        )
        assert resp.status_code == 200
        assert b"cost-dashboard" in resp.data


def _enroll(application, environment):
    db.session.add(
        ApplicationEnvironment(
            application_id=application.id,
            environment_id=environment.id,
            k8s_identifier=environment.k8s_identifier,
        )
    )
    db.session.commit()


class TestCostScopeSelection:
    def test_env_path_without_env_query_reports_all_environments(
        self, client, member_user, cost_context
    ):
        _login(client, member_user)
        org, project = cost_context["org"], cost_context["project"]
        application, environment = (
            cost_context["application"],
            cost_context["environment"],
        )
        _enroll(application, environment)
        base = f"/projects/{org.slug}/{project.slug}/env/{environment.slug}/applications/{application.slug}/cost/data"
        with patch(
            "cabotage.server.cost.views.build_report", return_value=_fake_report()
        ) as build:
            assert client.get(base).status_code == 200
            assert client.get(f"{base}?env={environment.slug}").status_code == 200
        assert build.call_args_list[0].args[0].environment is None
        assert build.call_args_list[1].args[0].environment.id == environment.id

    def test_unknown_env_returns_404(self, client, member_user, cost_context):
        _login(client, member_user)
        org, project = cost_context["org"], cost_context["project"]
        application = cost_context["application"]
        with patch(
            "cabotage.server.cost.views.build_report", return_value=_fake_report()
        ):
            app_resp = client.get(
                f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/cost/data?env=nope"
            )
            project_resp = client.get(
                f"/projects/{org.slug}/{project.slug}/cost/data?env=nope"
            )
        assert app_resp.status_code == 404
        assert project_resp.status_code == 404

    def test_unenrolled_env_page_returns_404(self, client, member_user, cost_context):
        _login(client, member_user)
        org, project = cost_context["org"], cost_context["project"]
        application, environment = (
            cost_context["application"],
            cost_context["environment"],
        )
        resp = client.get(
            f"/projects/{org.slug}/{project.slug}/env/{environment.slug}/applications/{application.slug}/cost"
        )
        assert resp.status_code == 404

    def test_out_of_range_custom_window_falls_back_to_preset(
        self, client, member_user, cost_context
    ):
        _login(client, member_user)
        org = cost_context["org"]
        with patch(
            "cabotage.server.cost.views.build_report", return_value=_fake_report()
        ) as build:
            resp = client.get(
                f"/organizations/{org.slug}/cost/data"
                "?start=9999-12-31T23:00:00&end=9999-12-31T23:59:59"
            )
        assert resp.status_code == 200
        assert "start" not in build.call_args.kwargs

    def test_custom_window_longer_than_max_span_falls_back_to_preset(
        self, client, member_user, cost_context
    ):
        _login(client, member_user)
        org = cost_context["org"]
        with patch(
            "cabotage.server.cost.views.build_report", return_value=_fake_report()
        ) as build:
            resp = client.get(
                f"/organizations/{org.slug}/cost/data"
                "?start=2020-01-01T00:00:00&end=2021-06-01T00:00:00"
            )
        assert resp.status_code == 200
        assert "start" not in build.call_args.kwargs


class TestCostRowLinks:
    def test_deleted_rows_and_their_movers_have_no_url(
        self, client, member_user, cost_context
    ):
        _login(client, member_user)
        org = cost_context["org"]
        report = _fake_report()
        report["breakdowns"]["projects"][0]["deleted"] = True
        report["movers"] = [{"key": "p1"}]
        with patch("cabotage.server.cost.views.build_report", return_value=report):
            data = client.get(f"/organizations/{org.slug}/cost/data").get_json()
        assert data["breakdowns"]["projects"][0]["url"] is None
        assert data["movers"][0]["url"] is None

    def test_platform_rows_link_only_to_viewable_orgs(
        self, client, admin_user, cost_context
    ):
        _login(client, admin_user)
        org = cost_context["org"]
        report = _fake_report()
        report["breakdowns"] = {
            "organizations": [
                {
                    "key": str(org.id),
                    "slugs": {"organization": org.slug},
                    "deleted": False,
                }
            ]
        }
        with patch("cabotage.server.cost.views.build_report", return_value=report):
            data = client.get("/infra/cost/data").get_json()
        assert data["breakdowns"]["organizations"][0]["url"] is None

        db.session.add(
            OrganizationMember(organization_id=org.id, user_id=admin_user.id)
        )
        db.session.commit()
        with patch("cabotage.server.cost.views.build_report", return_value=report):
            data = client.get("/infra/cost/data").get_json()
        assert data["breakdowns"]["organizations"][0]["url"] == (
            f"/organizations/{org.slug}/cost"
        )
