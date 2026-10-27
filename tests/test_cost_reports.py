"""Tests for cost report assembly (cabotage.server.applets.cost.reports)."""

import datetime
import itertools
import random
import uuid

import pytest
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.applets.cost.reports import CostScope, build_report
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.cost import (
    CostCollectionRun,
    CostLineItem,
    CostNode,
    CostNodeHour,
)
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Project,
)
from cabotage.server.models.resources import PostgresResource, Resource
from cabotage.server.wsgi import app as _app

NOW = datetime.datetime(2026, 10, 15, 10, 30, 0, tzinfo=datetime.UTC)


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["REQUIRE_MFA"] = False
    _app.config["SHOW_COST_ESTIMATES"] = True
    with _app.app_context():
        yield _app
    _app.config["REQUIRE_MFA"] = True
    _app.config["SHOW_COST_ESTIMATES"] = False


@pytest.fixture
def admin_user(app):
    user = User(
        username=f"reportadmin-{uuid.uuid4().hex[:8]}",
        email=f"reportadmin-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db.session.add(user)
    db.session.commit()
    yield user
    db.session.execute(
        db.text("DELETE FROM activity WHERE object_id = :uid"), {"uid": user.id}
    )
    db.session.execute(
        db.text("UPDATE transaction SET user_id = NULL WHERE user_id = :uid"),
        {"uid": user.id},
    )
    db.session.delete(user)
    db.session.commit()


@pytest.fixture
def org(admin_user):
    organization = Organization(
        name="Report Org", slug=f"report-org-{uuid.uuid4().hex[:8]}"
    )
    db.session.add(organization)
    db.session.flush()
    db.session.add(
        OrganizationMember(
            organization_id=organization.id, user_id=admin_user.id, admin=True
        )
    )
    db.session.commit()
    yield organization
    db.session.rollback()
    CostLineItem.query.filter_by(organization_id=organization.id).delete()
    for project in Project.query.filter_by(organization_id=organization.id).all():
        for environment in Environment.query.filter_by(project_id=project.id).all():
            for resource in Resource.query.filter_by(
                environment_id=environment.id
            ).all():
                if resource.type == "postgres":
                    db.session.execute(
                        db.text("DELETE FROM resources_postgres WHERE id = :rid"),
                        {"rid": resource.id},
                    )
                elif resource.type == "redis":
                    db.session.execute(
                        db.text("DELETE FROM resources_redis WHERE id = :rid"),
                        {"rid": resource.id},
                    )
            Resource.query.filter_by(environment_id=environment.id).delete()
            for application in Application.query.filter_by(project_id=project.id).all():
                ApplicationEnvironment.query.filter_by(
                    application_id=application.id
                ).delete()
            db.session.flush()
        Application.query.filter_by(project_id=project.id).delete()
        Environment.query.filter_by(project_id=project.id).delete()
    Project.query.filter_by(organization_id=organization.id).delete()
    OrganizationMember.query.filter_by(organization_id=organization.id).delete()
    db.session.flush()
    db.session.delete(organization)
    db.session.commit()


@pytest.fixture
def project(org):
    p = Project(name="Report Project", organization_id=org.id)
    db.session.add(p)
    db.session.flush()
    return p


@pytest.fixture
def environment(project):
    e = Environment(name="production", project_id=project.id)
    db.session.add(e)
    db.session.flush()
    return e


@pytest.fixture
def application(project):
    a = Application(name="API", slug="api", project_id=project.id)
    db.session.add(a)
    db.session.flush()
    return a


@pytest.fixture
def org2(admin_user):
    """A second, unrelated organization for platform-scope tests (no
    projects/environments/applications needed — only its id is used on
    CostLineItem rows directly).
    """
    organization = Organization(
        name="Second Report Org", slug=f"report-org2-{uuid.uuid4().hex[:8]}"
    )
    db.session.add(organization)
    db.session.flush()
    db.session.add(
        OrganizationMember(
            organization_id=organization.id, user_id=admin_user.id, admin=True
        )
    )
    db.session.commit()
    yield organization
    db.session.rollback()
    CostLineItem.query.filter_by(organization_id=organization.id).delete()
    OrganizationMember.query.filter_by(organization_id=organization.id).delete()
    db.session.flush()
    db.session.delete(organization)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean_cost_collection_runs(app):
    # CostCollectionRun is cluster-wide, not scoped to an organization, so
    # freshness/run-rate assertions need a clean table per test rather than
    # relying on the per-org CostLineItem cleanup in the `org` fixture.
    CostCollectionRun.query.delete()
    db.session.commit()
    yield
    CostCollectionRun.query.delete()
    db.session.commit()


def _add_run(period_start, status="succeeded", error=None, stats=None):
    run = CostCollectionRun.query.filter_by(period_start=period_start).first()
    if run is not None:
        return run
    run = CostCollectionRun(
        period_start=period_start,
        status=status,
        started_at=period_start + datetime.timedelta(hours=1, minutes=7),
        finished_at=period_start + datetime.timedelta(hours=1, minutes=7, seconds=30),
        error=error,
        rates={},
        stats=stats or {},
    )
    db.session.add(run)
    db.session.flush()
    return run


def _add_item(
    run,
    period_start,
    *,
    org,
    project=None,
    environment=None,
    application=None,
    resource=None,
    category="compute",
    meter="vcpu",
    component="web",
    quantity=1.0,
    unit="vCPU-hr",
    cost=1.0,
    usage=None,
    request=None,
):
    item = CostLineItem(
        run_id=run.id,
        period_start=period_start,
        organization_id=org.id,
        project_id=project.id if project else None,
        environment_id=environment.id if environment else None,
        application_id=application.id if application else None,
        resource_id=resource.id if resource else None,
        category=category,
        meter=meter,
        component=component,
        quantity=quantity,
        unit=unit,
        usage_quantity=usage,
        request_quantity=request,
        cost_usd=cost,
    )
    db.session.add(item)
    return item


def _add_hour_cost(hour, cost, **item_kwargs):
    _add_item(_add_run(hour), hour, cost=cost, **item_kwargs)


def _floor_hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def _add_node_hour(run, name, *, cost, idle, cpu_request):
    priced = cost is not None
    row = CostNodeHour(
        run_id=run.id,
        period_start=run.period_start,
        node_name=name,
        instance_type="m8g.xlarge" if priced else None,
        capacity_type="on-demand" if priced else None,
        cpu_capacity=4.0 if priced else None,
        memory_gib=16.0 if priced else None,
        node_hours=1.0,
        cpu_request_hours=cpu_request,
        cpu_usage_hours=cpu_request / 2,
        cpu_billed_hours=cpu_request,
        memory_request_gib_hours=8.0,
        memory_usage_gib_hours=4.0,
        memory_billed_gib_hours=8.0,
        cost_usd=cost,
        requested_cost_usd=(cost - idle) if priced else None,
        used_cost_usd=(cost - idle) / 2 if priced else None,
        idle_cost_usd=idle,
    )
    db.session.add(row)
    return row


class TestBuildReportBasics:
    def test_mtd_and_projection_math(self, app, org, project, environment, application):
        month_start = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # 24 succeeded hours before "now", each costing $2 (compute + memory).
        for i in range(24):
            hour = NOW.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(
                hours=i + 1
            )
            run = _add_run(hour)
            _add_item(
                run,
                hour,
                org=org,
                project=project,
                environment=environment,
                application=application,
                category="compute",
                meter="vcpu",
                cost=1.0,
            )
            _add_item(
                run,
                hour,
                org=org,
                project=project,
                environment=environment,
                application=application,
                category="compute",
                meter="memory",
                cost=1.0,
            )
        # A couple of extra hours earlier in the month, inside mtd but outside the 24h run-rate window.
        earlier = month_start + datetime.timedelta(hours=5)
        run2 = _add_run(earlier)
        _add_item(
            run2,
            earlier,
            org=org,
            project=project,
            environment=environment,
            application=application,
            cost=10.0,
        )
        db.session.commit()

        scope = CostScope(kind="organization", organization=org)
        report = build_report(scope, "mtd", now=NOW)

        mtd_cost = report["kpis"]["month_to_date"]
        assert mtd_cost == pytest.approx(24 * 2.0 + 10.0, rel=1e-6)

        run_rate_hourly = 24 * 2.0 / 24  # $2/hr over the last 24 succeeded hours
        hours_in_month = report["kpis"]["run_rate_monthly"] / run_rate_hourly
        elapsed_hours = (NOW - month_start).total_seconds() / 3600.0
        remaining_hours = hours_in_month - elapsed_hours
        expected_projection = mtd_cost + run_rate_hourly * remaining_hours
        assert report["kpis"]["projected_month"] == pytest.approx(
            expected_projection, rel=1e-6
        )

    def test_previous_period_change_pct(
        self, app, org, project, environment, application
    ):
        current_hour = NOW.replace(
            minute=0, second=0, microsecond=0
        ) - datetime.timedelta(hours=1)
        previous_hour = current_hour - datetime.timedelta(hours=24)

        run1 = _add_run(current_hour)
        _add_item(
            run1,
            current_hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            cost=15.0,
        )
        run2 = _add_run(previous_hour)
        _add_item(
            run2,
            previous_hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            cost=10.0,
        )
        db.session.commit()

        scope = CostScope(kind="organization", organization=org)
        report = build_report(scope, "24h", now=NOW)

        assert report["totals"]["cost"] == pytest.approx(15.0, rel=1e-6)
        assert report["totals"]["previous_cost"] == pytest.approx(10.0, rel=1e-6)
        assert report["totals"]["change_pct"] == pytest.approx(50.0, rel=1e-6)

    def test_change_pct_is_none_when_previous_is_zero(
        self, app, org, project, environment, application
    ):
        current_hour = NOW.replace(
            minute=0, second=0, microsecond=0
        ) - datetime.timedelta(hours=1)
        run = _add_run(current_hour)
        _add_item(
            run,
            current_hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            cost=5.0,
        )
        db.session.commit()

        scope = CostScope(kind="organization", organization=org)
        report = build_report(scope, "24h", now=NOW)
        assert report["totals"]["previous_cost"] == 0.0
        assert report["totals"]["change_pct"] is None

    def test_series_zero_filled_across_gaps(
        self, app, org, project, environment, application
    ):
        # Only one hour has data in an otherwise-empty 24h range.
        hour = NOW.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(
            hours=5
        )
        run = _add_run(hour)
        _add_item(
            run,
            hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            category="compute",
            cost=3.0,
        )
        db.session.commit()

        scope = CostScope(kind="organization", organization=org)
        report = build_report(scope, "24h", now=NOW)

        series = report["series"]
        assert len(series["timestamps"]) == 24
        for values in series["categories"].values():
            assert len(values) == 24
        nonzero = [v for v in series["categories"]["compute"] if v != 0.0]
        assert nonzero == [3.0]

    def test_freshness_stale_and_failed_hour(
        self, app, org, project, environment, application
    ):
        # Data stops 5 hours ago (> 2h stale threshold) and has one failed hour.
        last_good_hour = NOW.replace(
            minute=0, second=0, microsecond=0
        ) - datetime.timedelta(hours=5)
        run = _add_run(last_good_hour)
        _add_item(
            run,
            last_good_hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            cost=1.0,
        )
        failed_hour = NOW.replace(
            minute=0, second=0, microsecond=0
        ) - datetime.timedelta(hours=3)
        _add_run(failed_hour, status="failed", error="boom")
        db.session.commit()

        # Platform scope: missing_hours/failed_hours are platform-only.
        scope = CostScope(kind="platform")
        report = build_report(scope, "24h", now=NOW)
        freshness = report["freshness"]

        assert freshness["stale"] is True
        assert freshness["collection_enabled"] is True
        assert any(failed_hour.isoformat() == iso for iso in freshness["failed_hours"])
        # Hours between last_good_hour+1 and failed_hour-1, and failed_hour+1
        # through the most recently completed hour, have no run at all.
        assert freshness["missing_hours"] > 0

    def test_freshness_reports_failed_hours_from_preserved_data(
        self, app, org, project, environment, application
    ):
        """A preserved-on-failure hour (status still "succeeded" because the
        collector kept its prior good data, per collector._record_attempt)
        must still count as failed in `failed_hours`, since an hour's own
        `status` only describes the data it currently holds, not whether
        its most recent collection attempt actually succeeded.
        """
        floor_now = NOW.replace(minute=0, second=0, microsecond=0)
        healthy_hour = floor_now - datetime.timedelta(hours=4)
        streak_start_hour = floor_now - datetime.timedelta(hours=3)
        streak_end_hour = floor_now - datetime.timedelta(hours=2)

        healthy_run = _add_run(
            healthy_hour,
            stats={
                "last_attempt_status": "succeeded",
                "last_attempt_at": healthy_hour.isoformat(),
            },
        )
        _add_item(
            healthy_run,
            healthy_hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            cost=1.0,
        )
        streak_start_at = (healthy_hour + datetime.timedelta(minutes=5)).isoformat()
        # This hour's DATA is still "succeeded" (preserved from an earlier
        # good collection) even though the most recent attempt failed.
        preserved_run = _add_run(
            streak_start_hour,
            status="succeeded",
            stats={
                "last_attempt_status": "failed",
                "last_attempt_error": "no pod metrics for hour",
                "last_attempt_at": streak_start_at,
            },
        )
        _add_item(
            preserved_run,
            streak_start_hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            cost=1.0,
        )
        streak_end_at = (streak_end_hour + datetime.timedelta(minutes=5)).isoformat()
        _add_run(
            streak_end_hour,
            status="failed",
            error="no pod metrics for hour",
            stats={
                "last_attempt_status": "failed",
                "last_attempt_error": "no pod metrics for hour",
                "last_attempt_at": streak_end_at,
            },
        )
        db.session.commit()

        # Platform scope: missing_hours/failed_hours are platform-only.
        scope = CostScope(kind="platform")
        report = build_report(scope, "24h", now=NOW)
        freshness = report["freshness"]

        # The preserved hour still shows up as failed even though its own
        # `status` is "succeeded".
        assert streak_start_hour.isoformat() in freshness["failed_hours"]
        assert streak_end_hour.isoformat() in freshness["failed_hours"]
        assert healthy_hour.isoformat() not in freshness["failed_hours"]

    def test_breakdown_rows_for_each_scope(
        self, app, org, project, environment, application
    ):
        hour = NOW.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(
            hours=1
        )
        run = _add_run(hour)
        _add_item(
            run,
            hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            category="compute",
            component="web",
            cost=7.0,
        )
        resource = PostgresResource(
            environment_id=environment.id,
            name="DB",
            slug="db",
            service_version="18",
            size_class="db.small",
            storage_size=5,
        )
        db.session.add(resource)
        db.session.flush()
        _add_item(
            run,
            hour,
            org=org,
            project=project,
            environment=environment,
            resource=resource,
            category="backing_service",
            component="db",
            cost=2.0,
        )
        db.session.commit()

        org_report = build_report(
            CostScope(kind="organization", organization=org), "24h", now=NOW
        )
        assert set(org_report["breakdowns"].keys()) == {"projects", "applications"}
        assert org_report["breakdowns"]["projects"][0]["cost"] == pytest.approx(
            9.0, rel=1e-6
        )

        project_report = build_report(
            CostScope(kind="project", organization=org, project=project), "24h", now=NOW
        )
        assert set(project_report["breakdowns"].keys()) == {
            "environments",
            "applications",
        }

        env_report = build_report(
            CostScope(
                kind="environment",
                organization=org,
                project=project,
                environment=environment,
            ),
            "24h",
            now=NOW,
        )
        assert set(env_report["breakdowns"].keys()) == {
            "applications",
            "backing_services",
        }
        backing_rows = env_report["breakdowns"]["backing_services"]
        assert backing_rows and backing_rows[0]["cost"] == pytest.approx(2.0, rel=1e-6)

        app_report = build_report(
            CostScope(
                kind="application",
                organization=org,
                project=project,
                environment=environment,
                application=application,
            ),
            "24h",
            now=NOW,
        )
        assert set(app_report["breakdowns"].keys()) == {"environments", "processes"}
        process_rows = app_report["breakdowns"]["processes"]
        assert any(
            row["name"] == "web" and row["cost"] == pytest.approx(7.0, rel=1e-6)
            for row in process_rows
        )

    def test_custom_range_clamping(self, app, org, project, environment, application):
        hour = NOW.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(
            hours=2
        )
        run = _add_run(hour)
        _add_item(
            run,
            hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            cost=4.0,
        )
        db.session.commit()

        scope = CostScope(kind="organization", organization=org)
        report = build_report(
            scope,
            "custom",
            now=NOW,
            start="2000-01-01",
            end="2999-01-01",
        )
        range_start = datetime.datetime.fromisoformat(report["range"]["start"])
        range_end = datetime.datetime.fromisoformat(report["range"]["end"])
        # Clamped to extent (the one succeeded hour) on the low end, and to
        # "now" on the high end.
        assert range_start == hour
        assert range_end == NOW

    def test_series_arrays_are_aligned(
        self, app, org, project, environment, application
    ):
        hour = NOW.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(
            hours=1
        )
        run = _add_run(hour)
        _add_item(
            run,
            hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            usage=0.5,
            request=1.0,
            cost=1.0,
        )
        db.session.commit()

        scope = CostScope(kind="organization", organization=org)
        report = build_report(scope, "24h", now=NOW)
        series = report["series"]
        n = len(series["timestamps"])
        assert len(series["previous"]) == n
        assert len(series["efficiency"]["usage"]) == n
        assert len(series["efficiency"]["request"]) == n
        assert len(series["efficiency"]["billed"]) == n
        for breakdown in series["breakdowns"].values():
            assert len(breakdown["other"]) == n
            for values in breakdown["values"].values():
                assert len(values) == n

    def test_calendar_is_daily_for_the_last_90_days_of_long_history(
        self, app, org, project, environment, application
    ):
        old_hour = _floor_hour(NOW) - datetime.timedelta(days=200)
        recent_hour = _floor_hour(NOW) - datetime.timedelta(hours=2)
        _add_hour_cost(old_hour, 1.0, org=org, project=project)
        _add_hour_cost(recent_hour, 2.0, org=org, project=project)
        db.session.commit()

        report = build_report(
            CostScope(kind="organization", organization=org), "24h", now=NOW
        )
        extent_xs = report["extent_series"]["timestamps"]
        calendar_xs = report["calendar"]["timestamps"]
        assert extent_xs[1] - extent_xs[0] == 7 * 86400
        assert {b - a for a, b in itertools.pairwise(calendar_xs)} == {86400}
        assert len(calendar_xs) == 91
        assert sum(sum(v) for v in report["calendar"]["categories"].values()) == 2.0

    def test_heatmap_puts_cost_in_its_weekday_hour(
        self, app, org, project, environment, application
    ):
        hour = NOW.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(
            hours=1
        )
        run = _add_run(hour)
        _add_item(
            run,
            hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            cost=1.0,
        )
        db.session.commit()

        scope = CostScope(kind="organization", organization=org)
        report = build_report(scope, "24h", now=NOW)
        heatmap = report["heatmap"]
        assert heatmap["days"] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        assert heatmap["hours"] == list(range(24))

        # `_heatmap` averages cost per (weekday, hour) cell; a 24h range only
        # ever visits one period_start per (weekday, hour) pair, so the
        # inserted item's cell holds its cost and every other cell is 0.
        day_index = hour.weekday()
        for day, row in enumerate(heatmap["values"]):
            for col, value in enumerate(row):
                if day == day_index and col == hour.hour:
                    assert value == pytest.approx(1.0, rel=1e-6)
                else:
                    assert value == 0.0

    def test_platform_reconciliation_balances(
        self, app, org, project, environment, application
    ):
        hour = NOW.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(
            hours=1
        )
        run = _add_run(
            hour,
            stats={
                "seed": "demo",
                "cluster_compute_cost": 10.0,
                "fixed_overhead": 2.0,
                "pods_seen": 20,
                "pods_unallocated": 1,
            },
        )
        # tenant_direct and overhead_allocated are derived from line items
        # (the billing truth), not from the run's stats snapshot.
        _add_item(
            run,
            hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            category="compute",
            cost=6.0,
        )
        _add_item(
            run,
            hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            category="overhead",
            meter="shared",
            component="shared",
            cost=6.0,
        )
        db.session.commit()

        scope = CostScope(kind="platform")
        report = build_report(scope, "24h", now=NOW)
        rec = report["platform"]["reconciliation"]
        assert rec["cluster_compute"] == pytest.approx(10.0, rel=1e-6)
        assert rec["fixed_overhead"] == pytest.approx(2.0, rel=1e-6)
        assert rec["tenant_direct"] == pytest.approx(6.0, rel=1e-6)
        assert rec["overhead_allocated"] == pytest.approx(6.0, rel=1e-6)
        assert rec["unallocated_idle"] == pytest.approx(0.0, abs=1e-9)
        assert rec["total"] == pytest.approx(12.0, rel=1e-6)
        # tenant_direct + overhead_allocated + unallocated_idle == total,
        # within rounding.
        reconciled = (
            rec["tenant_direct"] + rec["overhead_allocated"] + rec["unallocated_idle"]
        )
        assert reconciled == pytest.approx(rec["total"], rel=1e-6)
        assert rec["residual"] == pytest.approx(0.0, abs=1e-6)

        unallocated = report["platform"]["unallocated"]
        assert unallocated["pods_seen"] == pytest.approx(20.0, rel=1e-6)
        assert unallocated["pods_unallocated"] == pytest.approx(1.0, rel=1e-6)
        assert unallocated["share"] == pytest.approx(0.05, rel=1e-6)

    def test_platform_totals_equal_sum_of_org_totals(
        self, app, org, project, environment, application, org2
    ):
        hour = NOW.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(
            hours=1
        )
        run = _add_run(hour)
        _add_item(
            run,
            hour,
            org=org,
            project=project,
            environment=environment,
            application=application,
            cost=5.0,
        )
        _add_item(run, hour, org=org2, cost=3.0, component="other")
        db.session.commit()

        platform_report = build_report(CostScope(kind="platform"), "24h", now=NOW)
        org1_report = build_report(
            CostScope(kind="organization", organization=org), "24h", now=NOW
        )
        org2_report = build_report(
            CostScope(kind="organization", organization=org2), "24h", now=NOW
        )

        assert platform_report["totals"]["cost"] == pytest.approx(
            org1_report["totals"]["cost"] + org2_report["totals"]["cost"], rel=1e-6
        )
        assert platform_report["totals"]["cost"] == pytest.approx(8.0, rel=1e-6)

        org_keys = {
            row["slugs"]["organization"]
            for row in platform_report["breakdowns"]["organizations"]
        }
        assert org.slug in org_keys
        assert org2.slug in org_keys

    def test_platform_capacity_never_below_requested(
        self, app, org, project, environment, application
    ):
        """Mirrors demo.finalize_platform_stats's formula (cluster_compute
        per hour = tenant_direct marked up by a 15-25% idle margin) across a
        full day of varying hourly billed cost, then checks a 30d (daily-
        bucketed) report never shows capacity_cost below requested_cost for
        any bucket -- the regression this guards is a day bucket summing a
        FIXED fleet-wide cluster_compute_cost against a VARIABLE diurnal
        tenant cost, which can dip below requested during peak hours.
        """
        day_start = (NOW - datetime.timedelta(days=5)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        rng = random.Random("capacity-invariant-test")
        for hour_offset in range(24):
            hour = day_start + datetime.timedelta(hours=hour_offset)
            # Diurnal swing: low overnight, high at midday.
            billed = 2.0 + 8.0 * abs(12 - hour_offset) / 12.0
            idle_fraction = rng.uniform(0.15, 0.25)
            cluster_compute_cost = billed / (1.0 - idle_fraction)
            run = _add_run(
                hour,
                stats={
                    "seed": "demo",
                    "cluster_compute_cost": cluster_compute_cost,
                    "fixed_overhead": 1.0,
                    "pods_seen": 100,
                    "pods_unallocated": 2,
                },
            )
            _add_item(
                run,
                hour,
                org=org,
                project=project,
                environment=environment,
                application=application,
                category="compute",
                meter="vcpu",
                quantity=billed,
                usage=billed * 0.7,
                request=billed * 0.9,
                cost=billed,
            )
        db.session.commit()

        report = build_report(CostScope(kind="platform"), "30d", now=NOW)
        capacity = report["platform"]["capacity_series"]
        for cap, req in zip(
            capacity["capacity_cost"], capacity["requested_cost"], strict=True
        ):
            assert cap >= req - 1e-9

    @pytest.mark.parametrize("range_key", ["30d", "90d", "mtd"])
    def test_daily_series_sum_to_totals_and_align_previous(
        self, app, org, project, environment, application, range_key
    ):
        if range_key == "mtd":
            start = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            start = NOW - datetime.timedelta(days=int(range_key[:-1]))
        first_hour = _floor_hour(start) + datetime.timedelta(hours=1)
        scope_items = {
            "org": org,
            "project": project,
            "environment": environment,
            "application": application,
        }
        _add_hour_cost(first_hour, 5.0, **scope_items)
        _add_hour_cost(
            _floor_hour(NOW) - datetime.timedelta(hours=1), 3.0, **scope_items
        )
        _add_hour_cost(
            _floor_hour(start) - datetime.timedelta(hours=1), 4.0, **scope_items
        )
        db.session.commit()

        report = build_report(
            CostScope(kind="organization", organization=org), range_key, now=NOW
        )
        series = report["series"]
        series_total = sum(sum(values) for values in series["categories"].values())
        assert series_total == pytest.approx(report["totals"]["cost"], rel=1e-6)
        assert report["totals"]["cost"] == pytest.approx(8.0, rel=1e-6)
        assert len(series["previous"]) == len(series["timestamps"])
        assert sum(series["previous"]) == pytest.approx(
            report["totals"]["previous_cost"], rel=1e-6
        )

    def test_daily_series_ignore_session_timezone(
        self, app, org, project, environment, application
    ):
        hour = _floor_hour(NOW) - datetime.timedelta(days=5)
        _add_hour_cost(hour, 5.0, org=org, project=project, application=application)
        db.session.commit()

        db.session.execute(db.text("SET TIME ZONE 'America/New_York'"))
        try:
            report = build_report(
                CostScope(kind="organization", organization=org), "30d", now=NOW
            )
        finally:
            db.session.execute(db.text("SET TIME ZONE 'UTC'"))

        series = report["series"]
        assert sum(sum(v) for v in series["categories"].values()) == pytest.approx(5.0)

    def test_movers_include_drop_to_zero(
        self, app, org, project, environment, application
    ):
        dropped = Project(name="Dropped Project", organization_id=org.id)
        db.session.add(dropped)
        db.session.flush()
        current_hour = _floor_hour(NOW) - datetime.timedelta(hours=1)
        previous_hour = current_hour - datetime.timedelta(hours=24)
        _add_hour_cost(current_hour, 11.0, org=org, project=project)
        run = _add_run(previous_hour)
        _add_item(run, previous_hour, org=org, project=project, cost=10.0)
        _add_item(run, previous_hour, org=org, project=dropped, cost=500.0)
        db.session.commit()

        report = build_report(
            CostScope(kind="organization", organization=org), "24h", now=NOW
        )
        top = report["movers"][0]
        assert top["key"] == str(dropped.id)
        assert top["cost"] == 0.0
        assert top["change"] == pytest.approx(-500.0, rel=1e-6)

    def test_freshness_ignores_partial_start_hour(self, app):
        range_start_floor = _floor_hour(NOW - datetime.timedelta(hours=24))
        _add_run(range_start_floor - datetime.timedelta(hours=2))
        hour = range_start_floor + datetime.timedelta(hours=1)
        while hour < _floor_hour(NOW):
            _add_run(hour)
            hour += datetime.timedelta(hours=1)
        db.session.commit()

        report = build_report(CostScope(kind="platform"), "24h", now=NOW)
        assert report["freshness"]["missing_hours"] == 0

    def test_freshness_ignores_hours_before_first_collection(self, app):
        for offset in range(1, 6):
            _add_run(_floor_hour(NOW) - datetime.timedelta(hours=offset))
        db.session.commit()

        report = build_report(CostScope(kind="platform"), "90d", now=NOW)
        assert report["freshness"]["missing_hours"] == 0

    def test_rates_only_list_recently_seen_node_types(self, app):
        suffix = uuid.uuid4().hex[:8]
        stale = CostNode(
            name=f"stale-{suffix}",
            instance_type="r8g.xlarge",
            capacity_type="on-demand",
            cpu_capacity=4,
            memory_gib=32,
            last_seen=NOW - datetime.timedelta(days=60),
        )
        live = CostNode(
            name=f"live-{suffix}",
            instance_type="m7i.large",
            capacity_type="on-demand",
            cpu_capacity=2,
            memory_gib=8,
            last_seen=NOW - datetime.timedelta(hours=1),
        )
        db.session.add_all([stale, live])
        db.session.commit()
        try:
            report = build_report(CostScope(kind="platform"), "24h", now=NOW)
        finally:
            CostNode.query.filter(CostNode.name.in_([stale.name, live.name])).delete()
            db.session.commit()

        rate_nodes = {n["instance_type"]: n["hourly"] for n in report["rates"]["nodes"]}
        assert "r8g.xlarge" not in rate_nodes
        assert rate_nodes["m7i.large"] > 0

    def test_platform_nodes_sum_node_hours_in_range(self, app):
        hour = _floor_hour(NOW) - datetime.timedelta(hours=2)
        outside = _floor_hour(NOW) - datetime.timedelta(hours=30)
        for period_start, cost in (
            (hour, 1.0),
            (hour - datetime.timedelta(hours=1), 1.0),
        ):
            run = _add_run(period_start)
            _add_node_hour(run, "busy", cost=cost, idle=0.25, cpu_request=3.0)
            _add_node_hour(run, "mystery", cost=None, idle=None, cpu_request=1.0)
        _add_node_hour(_add_run(outside), "busy", cost=50.0, idle=50.0, cpu_request=0.0)
        db.session.commit()

        report = build_report(CostScope(kind="platform"), "24h", now=NOW)
        nodes = {n["name"]: n for n in report["platform"]["nodes"]}

        assert list(nodes) == ["busy", "mystery"]
        busy = nodes["busy"]
        assert busy["node_hours"] == pytest.approx(2.0)
        assert busy["cost"] == pytest.approx(2.0)
        assert busy["idle_cost"] == pytest.approx(0.5)
        # Hour-weighted average, not the summed vCPU-hours.
        assert busy["cpu_requested"] == pytest.approx(3.0)
        assert busy["cpu_capacity"] == 4.0
        mystery = nodes["mystery"]
        assert mystery["cost"] is None
        assert mystery["idle_cost"] is None
        assert mystery["cpu_requested"] == pytest.approx(1.0)

    def test_platform_node_inventory_reports_latest_run(self, app):
        _add_run(
            _floor_hour(NOW) - datetime.timedelta(hours=3),
            stats={"node_inventory_error": "old", "nodes_unpriced": 9},
        )
        latest = _floor_hour(NOW) - datetime.timedelta(hours=2)
        _add_run(
            latest,
            stats={
                "node_inventory_error": "listing nodes failed: 403 Forbidden",
                "nodes_unpriced": 2,
            },
        )
        _add_run(_floor_hour(NOW) - datetime.timedelta(hours=1), status="failed")
        db.session.commit()

        report = build_report(CostScope(kind="platform"), "24h", now=NOW)

        assert report["platform"]["node_inventory"] == {
            "period_start": latest.isoformat(),
            "error": "listing nodes failed: 403 Forbidden",
            "nodes_unpriced": 2,
        }

    def test_tenant_scope_omits_install_wide_fields(
        self, app, org, project, environment, application
    ):
        """Organization (tenant) scope must not leak install-wide data:
        cluster node instance types in `rates.nodes`, or collector-health
        detail in `freshness.missing_hours`/`failed_hours`. Platform scope
        keeps all of it.
        """
        node = CostNode(
            name=f"node-{uuid.uuid4().hex[:8]}",
            instance_type="m7i.large",
            capacity_type="on-demand",
            cpu_capacity=2,
            memory_gib=8,
            last_seen=NOW - datetime.timedelta(hours=1),
        )
        db.session.add(node)
        _add_run(
            NOW.replace(minute=0, second=0, microsecond=0)
            - datetime.timedelta(hours=3),
            status="failed",
            error="boom",
        )
        db.session.commit()
        try:
            tenant_scope = CostScope(kind="organization", organization=org)
            tenant_report = build_report(tenant_scope, "24h", now=NOW)
            platform_report = build_report(CostScope(kind="platform"), "24h", now=NOW)
        finally:
            CostNode.query.filter_by(name=node.name).delete()
            db.session.commit()

        assert "nodes" not in tenant_report["rates"]
        assert "missing_hours" not in tenant_report["freshness"]
        assert "failed_hours" not in tenant_report["freshness"]
        for key in ("collection_enabled", "stale", "last_run_at", "data_through"):
            assert key in tenant_report["freshness"]

        assert "nodes" in platform_report["rates"]
        assert "missing_hours" in platform_report["freshness"]
        assert "failed_hours" in platform_report["freshness"]
