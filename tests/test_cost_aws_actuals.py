"""Tests for cabotage.server.applets.cost.aws_actuals."""

import datetime
import uuid

import pytest

from cabotage.server import db
from cabotage.server.applets.cost.aws_actuals import (
    actuals_rate_overrides,
    bucket_for_usage_type,
    refresh_actuals,
)
from cabotage.server.applets.cost.collector import _write_run
from cabotage.server.applets.cost.rates import effective_rates
from cabotage.server.models.auth import Organization
from cabotage.server.models.cost import CostAwsDaily, CostCollectionRun, CostLineItem
from cabotage.server.models.projects import Application, Project
from cabotage.server.wsgi import app as _app

TODAY = datetime.date(2026, 10, 15)
DAY = TODAY - datetime.timedelta(days=2)  # the newest settled day
DAY_START = datetime.datetime.combine(DAY, datetime.time(), datetime.UTC)


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    with _app.app_context():
        yield _app


@pytest.fixture(autouse=True)
def _clean(app):
    CostAwsDaily.query.delete()
    CostCollectionRun.query.delete()
    db.session.commit()
    yield
    db.session.rollback()
    CostLineItem.query.filter(CostLineItem.aws_day.isnot(None)).delete()
    CostAwsDaily.query.delete()
    CostCollectionRun.query.delete()
    db.session.commit()


@pytest.fixture
def org(app):
    organization = Organization(
        name="Actuals Org", slug=f"actuals-org-{uuid.uuid4().hex[:8]}"
    )
    db.session.add(organization)
    db.session.commit()
    yield organization
    db.session.rollback()
    CostLineItem.query.filter_by(organization_id=organization.id).delete()
    for project in Project.query.filter_by(organization_id=organization.id).all():
        Application.query.filter_by(project_id=project.id).delete()
    Project.query.filter_by(organization_id=organization.id).delete()
    db.session.delete(organization)
    db.session.commit()


@pytest.fixture
def apps(org):
    project = Project(name="Actuals Project", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    web = Application(name="Web", slug="web", project_id=project.id)
    api = Application(name="API", slug="api", project_id=project.id)
    db.session.add_all([web, api])
    db.session.flush()
    return web, api


class FakeCostExplorer:
    """Serves ``pages`` in order, one per ``get_cost_and_usage`` call."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    def get_cost_and_usage(self, **request):
        self.requests.append(request)
        return self.pages[len(self.requests) - 1]


def _page(groups_by_day, token=None):
    page = {
        "ResultsByTime": [
            {
                "TimePeriod": {
                    "Start": day.isoformat(),
                    "End": (day + datetime.timedelta(days=1)).isoformat(),
                },
                "Groups": [
                    {
                        "Keys": [usage_type],
                        "Metrics": {"AmortizedCost": {"Amount": str(amount)}},
                    }
                    for usage_type, amount in groups.items()
                ],
            }
            for day, groups in groups_by_day.items()
        ]
    }
    if token:
        page["NextPageToken"] = token
    return page


def _run(hour, stats=None):
    run = CostCollectionRun(
        period_start=hour,
        status="succeeded",
        started_at=hour,
        finished_at=hour,
        rates={},
        stats=stats or {},
    )
    db.session.add(run)
    db.session.flush()
    return run


def _item(run, *, org, application, category, cost, quantity=1.0, meter="vcpu"):
    db.session.add(
        CostLineItem(
            run_id=run.id,
            period_start=run.period_start,
            organization_id=org.id,
            project_id=application.project_id,
            application_id=application.id,
            category=category,
            meter=meter,
            component="web",
            quantity=quantity,
            unit="x",
            cost_usd=cost,
        )
    )


def _true_ups(category):
    return {
        (row.application_id, row.period_start): float(row.cost_usd)
        for row in CostLineItem.query.filter(
            CostLineItem.aws_day == DAY, CostLineItem.category == category
        ).all()
    }


@pytest.mark.parametrize(
    ("usage_type", "bucket"),
    [
        ("USE2-BoxUsage:m8g.2xlarge", "compute"),
        ("USE2-EBS:VolumeUsage.gp3", "storage"),
        ("USE2-DataTransfer-Regional-Bytes", "cross_az"),
        ("USE2-USW2-AWS-Out-Bytes", "inter_region"),
        ("USE2-DataTransfer-Out-Bytes", "internet_egress"),
        ("USE2-LCUUsage", "load_balancer"),
        ("USE2-PublicIPv4:InUseAddress", "public_ipv4"),
        ("USE2-CW:Requests", "other"),
    ],
)
def test_usage_types_map_to_buckets(usage_type, bucket):
    assert bucket_for_usage_type(usage_type) == bucket


def test_refresh_follows_pages_and_replaces_the_window(app):
    stale = DAY - datetime.timedelta(days=2)
    db.session.add(
        CostAwsDaily(day=stale, bucket="compute", cost_usd=1.0, fetched_at=DAY_START)
    )
    db.session.commit()
    client = FakeCostExplorer(
        [
            _page({DAY: {"USE2-BoxUsage:m8g.2xlarge": 100.0}}, token="next"),
            _page(
                {
                    DAY: {
                        "USE2-BoxUsage:c8g.xlarge": 10.0,
                        "USE2-DataTransfer-Regional-Bytes": 50.0,
                        "USE2-DataTransfer-In-Bytes": 0.0,
                    },
                    # Still settling: neither stored nor trued up.
                    TODAY - datetime.timedelta(days=1): {
                        "USE2-BoxUsage:m8g.2xlarge": 5.0
                    },
                }
            ),
        ]
    )

    days = refresh_actuals(client, today=TODAY)

    assert days == [DAY]
    assert client.requests[1]["NextPageToken"] == "next"
    assert client.requests[0]["TimePeriod"] == {
        "Start": (TODAY - datetime.timedelta(days=7)).isoformat(),
        "End": (TODAY - datetime.timedelta(days=1)).isoformat(),
    }
    stored = {(row.day, row.bucket): float(row.cost_usd) for row in CostAwsDaily.query}
    # The stale in-window day is gone (Cost Explorer no longer reports it).
    assert stored == {(DAY, "compute"): 110.0, (DAY, "cross_az"): 50.0}


def test_true_up_spreads_network_by_gb_and_the_rest_by_direct_cost(app, org, apps):
    web, api = apps
    h1, h2 = (
        DAY_START + datetime.timedelta(hours=1),
        DAY_START + datetime.timedelta(hours=2),
    )
    run1, run2 = _run(h1), _run(h2)
    # Estimates for the day: $10 direct + $2 overhead, $1 network.
    _item(run1, org=org, application=web, category="compute", cost=6.0)
    _item(run2, org=org, application=api, category="compute", cost=4.0)
    _item(run1, org=org, application=web, category="overhead", cost=2.0, meter="shared")
    _item(
        run1,
        org=org,
        application=web,
        category="network",
        cost=0.5,
        quantity=30.0,
        meter="network",
    )
    _item(
        run2,
        org=org,
        application=api,
        category="network",
        cost=0.5,
        quantity=10.0,
        meter="network",
    )
    db.session.commit()
    # Full-day actuals; 2 of 24 hours were collected, so a twelfth applies.
    client = FakeCostExplorer(
        [
            _page(
                {
                    DAY: {
                        "USE2-BoxUsage:m8g.2xlarge": 180.0,  # other: 15 vs 12 est.
                        "USE2-DataTransfer-Regional-Bytes": 60.0,  # network: 5 vs 1
                    }
                }
            )
        ]
    )

    refresh_actuals(client, today=TODAY)

    assert _true_ups("network") == {
        (web.id, h1): pytest.approx(3.0),
        (api.id, h2): pytest.approx(1.0),
    }
    assert _true_ups("overhead") == {
        (web.id, h1): pytest.approx(1.8),
        (api.id, h2): pytest.approx(1.2),
    }
    day_total = sum(
        float(row.cost_usd)
        for row in CostLineItem.query.filter(
            CostLineItem.period_start >= DAY_START,
            CostLineItem.period_start < DAY_START + datetime.timedelta(days=1),
        )
    )
    assert day_total == pytest.approx(20.0)

    # Refreshing again replaces the day's true-ups instead of stacking them.
    refresh_actuals(FakeCostExplorer(client.pages), today=TODAY)
    assert sum(_true_ups("network").values()) == pytest.approx(4.0)

    # A day Cost Explorer stops reporting loses its true-ups too.
    refresh_actuals(FakeCostExplorer([_page({})]), today=TODAY)
    assert CostLineItem.query.filter(CostLineItem.aws_day.isnot(None)).count() == 0


def test_network_true_up_falls_back_to_egress_usage(app, org, apps):
    web, api = apps
    run = _run(DAY_START)
    _item(run, org=org, application=web, category="compute", cost=1.0)
    _item(
        run,
        org=org,
        application=api,
        category="egress",
        cost=0.9,
        quantity=10.0,
        meter="egress",
    )
    db.session.commit()

    refresh_actuals(
        FakeCostExplorer([_page({DAY: {"USE2-DataTransfer-Out-Bytes": 2.9 * 24}})]),
        today=TODAY,
    )

    assert _true_ups("network") == {(api.id, DAY_START): pytest.approx(2.0)}


def test_true_ups_survive_recollecting_their_hour(app, org, apps):
    web, _ = apps
    run = _run(DAY_START)
    _item(run, org=org, application=web, category="compute", cost=1.0)
    db.session.commit()
    refresh_actuals(
        FakeCostExplorer([_page({DAY: {"USE2-BoxUsage:m8g.2xlarge": 3.0 * 24}})]),
        today=TODAY,
    )

    _write_run(
        DAY_START,
        DAY_START,
        effective_rates({}),
        status="succeeded",
        error=None,
        stats={},
        line_items=[],
        node_hours=[],
    )

    assert _true_ups("overhead") == {(web.id, DAY_START): pytest.approx(2.0)}


def test_rate_overrides_from_latest_actuals(app, org, apps):
    web, _ = apps
    for hour in range(12):
        run = _run(DAY_START + datetime.timedelta(hours=hour))
        _item(run, org=org, application=web, category="storage", cost=0.1)
        _item(
            run,
            org=org,
            application=web,
            category="network",
            cost=0.0,
            quantity=100.0,
            meter="network",
        )
    for bucket, cost in {
        "cross_az": 24.0,
        "load_balancer": 4.8,
        "storage": 4.8,
        "compute": 100.0,
    }.items():
        db.session.add(
            CostAwsDaily(day=DAY, bucket=bucket, cost_usd=cost, fetched_at=DAY_START)
        )
    db.session.commit()

    overrides = actuals_rate_overrides(today=TODAY)
    rates = effective_rates({"COST_AWS_ACTUALS_ENABLED": True})

    # $24 cross-AZ over 1,200 tenant GB in 12 hours, i.e. 2,400 GB for the day.
    assert overrides["network_gb"] == pytest.approx(0.01)
    # $4.80 EBS minus $2.40/day already priced as storage line items.
    assert overrides["fixed_hourly"] == {
        "load_balancer": pytest.approx(0.2),
        "unattributed_storage": pytest.approx(0.1),
    }
    assert rates["fixed_hourly"] == overrides["fixed_hourly"]
    assert rates["network_gb"] == pytest.approx(0.01)
    # A day later than settled: rates don't come from it.
    assert actuals_rate_overrides(today=TODAY - datetime.timedelta(days=1)) == {}


def test_no_rate_overrides_without_collected_hours(app):
    db.session.add(
        CostAwsDaily(day=DAY, bucket="cross_az", cost_usd=24.0, fetched_at=DAY_START)
    )
    db.session.commit()

    assert actuals_rate_overrides() == {}
    assert (
        "eks_control_plane"
        in effective_rates({"COST_AWS_ACTUALS_ENABLED": True})["fixed_hourly"]
    )
