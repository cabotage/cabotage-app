"""Tests for the hourly cost collector (cabotage.utils.cost.collector)."""

import datetime
import uuid
from unittest.mock import patch

import pytest
from flask_security import hash_password

from cabotage.celery.tasks.cost import FAILED_HOUR_RETRY_AFTER, collect_costs
from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.cost import CostCollectionRun, CostLineItem, CostNode
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Image,
    Project,
)
from cabotage.server.models.resources import PostgresResource, RedisResource, Resource
from cabotage.server.models.utils import safe_k8s_name
from cabotage.server.wsgi import app as _app
from cabotage.utils.cost.collector import collect_hour
from cabotage.utils.mimir import MimirError

PERIOD_START = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["REQUIRE_MFA"] = False
    _app.config["MIMIR_URL"] = "https://mimir.example.test"
    _app.config["KUBERNETES_ENABLED"] = False
    with _app.app_context():
        yield _app
    _app.config["REQUIRE_MFA"] = True
    _app.config["MIMIR_URL"] = None


@pytest.fixture(autouse=True)
def _clean_period_start_run(app):
    """CostCollectionRun is cluster-wide (one row per period_start), and every
    test in this file reuses the same PERIOD_START, so a leftover run from a
    prior test would otherwise be picked up by collect_hour's preserve-on-
    failure logic. CostLineItem cascades via ON DELETE CASCADE.
    """
    CostCollectionRun.query.filter_by(period_start=PERIOD_START).delete()
    db.session.commit()
    yield
    CostCollectionRun.query.filter_by(period_start=PERIOD_START).delete()
    db.session.commit()


@pytest.fixture
def admin_user(app):
    user = User(
        username=f"costadmin-{uuid.uuid4().hex[:8]}",
        email=f"costadmin-{uuid.uuid4().hex[:8]}@example.com",
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
        name="Cost Org", slug=f"cost-org-{uuid.uuid4().hex[:8]}"
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
                child = {"postgres": PostgresResource, "redis": RedisResource}.get(
                    resource.type
                )
                if child is not None:
                    table = child.__table__
                    db.session.execute(table.delete().where(table.c.id == resource.id))
            Resource.query.filter_by(environment_id=environment.id).delete()
            for application in Application.query.filter_by(project_id=project.id).all():
                Image.query.filter_by(application_id=application.id).delete()
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
    p = Project(name="Cost Project", organization_id=org.id)
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
def app_env(application, environment):
    ae = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
        k8s_identifier=environment.k8s_identifier,
    )
    db.session.add(ae)
    db.session.flush()
    return ae


def _series(namespace, pod, points):
    """Build one Mimir range-query series: points = [(offset_seconds, value)]."""
    base = int(PERIOD_START.timestamp())
    return {
        "metric": {"namespace": namespace, "pod": pod},
        "values": [[base + offset, str(value)] for offset, value in points],
    }


def _full_hour(value):
    return [(step * 300, value) for step in range(12)]


def _second_half_hour(value):
    return [(step * 300, value) for step in range(6, 12)]


GIB = 1024.0**3


def _mimir_stub(responses, pod_labels, traefik=None, node_presence=None, raise_on=None):
    """responses: dict query-substring -> list[series]."""

    def query_range(query, start, end, step, tenant_id=None):
        if raise_on and raise_on in query:
            raise MimirError("boom")
        for substr, result in responses.items():
            if substr in query:
                return result
        if "count by (instance)" in query:
            return node_presence or []
        return []

    def query_instant(query, tenant_id=None, time=None):
        if "kube_pod_labels" in query:
            return pod_labels
        if "traefik" in query:
            return traefik or []
        return []

    return query_range, query_instant


def _app_pod_responses(namespace, pod):
    return {
        "rate(container_cpu_usage_seconds_total": [
            _series(namespace, pod, _full_hour(0.5))
        ],
        "container_memory_working_set_bytes{": [
            _series(namespace, pod, _full_hour(0.5 * GIB))
        ],
        'resource="cpu"': [_series(namespace, pod, _full_hour(1.0))],
        'resource="memory"': [_series(namespace, pod, _full_hour(1.0 * GIB))],
    }


def _app_pod_labels(org, project, application, pod):
    return [
        {
            "metric": {
                "namespace": org.k8s_identifier,
                "pod": pod,
                "label_cabotage_io_organization": org.k8s_identifier,
                "label_cabotage_io_project": project.k8s_identifier,
                "label_cabotage_io_application": application.k8s_identifier,
                "label_process": "web",
            }
        }
    ]


def _collect_with(responses, pod_labels):
    query_range, query_instant = _mimir_stub(responses, pod_labels)
    with (
        patch("cabotage.utils.mimir.query_range", side_effect=query_range),
        patch("cabotage.utils.mimir.query_instant", side_effect=query_instant),
    ):
        return collect_hour(PERIOD_START)


class TestCostCollector:
    def test_max_request_usage_and_partial_hour_pod(
        self, app, org, project, environment, application, app_env
    ):
        namespace = org.k8s_identifier

        # Full-hour pod: usage below request for 6 steps, above for 6 steps.
        cpu_usage_full = _series(
            namespace,
            "api-web-full",
            [(s * 300, 0.5 if s < 6 else 1.5) for s in range(12)],
        )
        cpu_request_full = _series(namespace, "api-web-full", _full_hour(1.0))
        mem_usage_full = _series(namespace, "api-web-full", _full_hour(0.8 * GIB))
        mem_request_full = _series(namespace, "api-web-full", _full_hour(1.0 * GIB))

        # Partial-hour pod: only present for the second half of the hour.
        cpu_usage_partial = _series(
            namespace, "api-web-partial", _second_half_hour(0.1)
        )
        cpu_request_partial = _series(
            namespace, "api-web-partial", _second_half_hour(0.2)
        )
        mem_usage_partial = _series(
            namespace, "api-web-partial", _second_half_hour(0.3 * GIB)
        )
        mem_request_partial = _series(
            namespace, "api-web-partial", _second_half_hour(0.4 * GIB)
        )

        pod_labels = [
            {
                "metric": {
                    "namespace": namespace,
                    "pod": "api-web-full",
                    "label_cabotage_io_organization": org.k8s_identifier,
                    "label_cabotage_io_project": project.k8s_identifier,
                    "label_cabotage_io_application": application.k8s_identifier,
                    "label_process": "web",
                }
            },
            {
                "metric": {
                    "namespace": namespace,
                    "pod": "api-web-partial",
                    "label_cabotage_io_organization": org.k8s_identifier,
                    "label_cabotage_io_project": project.k8s_identifier,
                    "label_cabotage_io_application": application.k8s_identifier,
                    "label_process": "web",
                }
            },
        ]

        responses = {
            "rate(container_cpu_usage_seconds_total": [
                cpu_usage_full,
                cpu_usage_partial,
            ],
            "container_memory_working_set_bytes{": [mem_usage_full, mem_usage_partial],
            'resource="cpu"': [cpu_request_full, cpu_request_partial],
            'resource="memory"': [mem_request_full, mem_request_partial],
        }
        query_range, query_instant = _mimir_stub(responses, pod_labels)

        with (
            patch("cabotage.utils.mimir.query_range", side_effect=query_range),
            patch("cabotage.utils.mimir.query_instant", side_effect=query_instant),
        ):
            run = collect_hour(PERIOD_START)

        assert run.status == "succeeded"
        items = CostLineItem.query.filter_by(run_id=run.id).all()
        cpu_items = {
            i.component: i
            for i in items
            if i.meter == "vcpu" and i.category == "compute"
        }

        # Both pods share component "web" (same app+process), so their
        # billed/usage/request quantities are aggregated into one row:
        # full pod billed  = (1.0*6 + 1.5*6) * 5/60 = 1.25 vCPU-hr
        # partial pod billed = max(0.2,0.1)*6 * 5/60              = 0.10 vCPU-hr
        # full pod usage   = (0.5*6 + 1.5*6) * 5/60 = 1.00 vCPU-hr
        # partial pod usage   = 0.1*6 * 5/60                      = 0.05 vCPU-hr
        # full pod request = 1.0*12 * 5/60          = 1.00 vCPU-hr
        # partial pod request = 0.2*6 * 5/60                      = 0.10 vCPU-hr
        assert cpu_items["web"].quantity == pytest.approx(1.35, rel=1e-6)
        assert cpu_items["web"].usage_quantity == pytest.approx(1.05, rel=1e-6)
        assert cpu_items["web"].request_quantity == pytest.approx(1.10, rel=1e-6)

    def test_pod_present_for_part_of_hour_only_bills_its_lifetime(
        self, app, org, project, environment, application, app_env
    ):
        namespace = org.k8s_identifier
        cpu_usage = _series(namespace, "api-web-partial", _second_half_hour(0.1))
        cpu_request = _series(namespace, "api-web-partial", _second_half_hour(0.2))
        mem_usage = _series(namespace, "api-web-partial", _second_half_hour(0.3 * GIB))
        mem_request = _series(
            namespace, "api-web-partial", _second_half_hour(0.4 * GIB)
        )
        pod_labels = [
            {
                "metric": {
                    "namespace": namespace,
                    "pod": "api-web-partial",
                    "label_cabotage_io_organization": org.k8s_identifier,
                    "label_cabotage_io_project": project.k8s_identifier,
                    "label_cabotage_io_application": application.k8s_identifier,
                    "label_process": "web",
                }
            }
        ]
        responses = {
            "rate(container_cpu_usage_seconds_total": [cpu_usage],
            "container_memory_working_set_bytes{": [mem_usage],
            'resource="cpu"': [cpu_request],
            'resource="memory"': [mem_request],
        }
        query_range, query_instant = _mimir_stub(responses, pod_labels)
        with (
            patch("cabotage.utils.mimir.query_range", side_effect=query_range),
            patch("cabotage.utils.mimir.query_instant", side_effect=query_instant),
        ):
            run = collect_hour(PERIOD_START)

        items = CostLineItem.query.filter_by(run_id=run.id, meter="vcpu").all()
        assert len(items) == 1
        # Only 6 of 12 steps have data: billed = max(0.2, 0.1) * 6 * 5/60 = 0.1
        assert items[0].quantity == pytest.approx(0.1, rel=1e-6)

    def test_attribution_app_build_backing_and_unallocated(
        self, app, org, project, environment, application, app_env
    ):
        namespace = org.k8s_identifier

        # App compute pod.
        app_pod = "api-web-abc12-xyz34"
        # Build pod (image build job). Use a fresh id each run: build_job_id
        # has no DB-level uniqueness constraint, so a hardcoded literal could
        # collide with rows left behind by other tests/runs.
        build_job_id = uuid.uuid4().hex[:8]
        image = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name="registry/test/api",
            build_ref="main",
            build_job_id=build_job_id,
        )
        db.session.add(image)
        db.session.flush()
        build_pod = f"imagebuild-{build_job_id}-f00ba"

        # Backing service (postgres) pod.
        resource = PostgresResource(
            environment_id=environment.id,
            name="maindb",
            service_version="18",
            size_class="db.medium",
            storage_size=10,
        )
        db.session.add(resource)
        db.session.flush()
        backing_pod = (
            f"{safe_k8s_name(project.k8s_identifier, resource.k8s_identifier)}-0"
        )

        # Unallocated: no cabotage.io/* labels at all.
        unalloc_pod = "coredns-7d8f9c-abcde"

        def mk(pod, value):
            s = _series(namespace, pod, _full_hour(value))
            return s

        responses = {
            "rate(container_cpu_usage_seconds_total": [
                mk(app_pod, 0.1),
                mk(build_pod, 0.05),
                mk(backing_pod, 0.2),
                mk(unalloc_pod, 0.01),
            ],
            "container_memory_working_set_bytes{": [
                mk(app_pod, 0.2 * GIB),
                mk(build_pod, 0.1 * GIB),
                mk(backing_pod, 0.3 * GIB),
                mk(unalloc_pod, 0.05 * GIB),
            ],
            'resource="cpu"': [
                mk(app_pod, 0.2),
                mk(build_pod, 0.1),
                mk(backing_pod, 0.3),
                mk(unalloc_pod, 0.02),
            ],
            'resource="memory"': [
                mk(app_pod, 0.25 * GIB),
                mk(build_pod, 0.15 * GIB),
                mk(backing_pod, 0.35 * GIB),
                mk(unalloc_pod, 0.06 * GIB),
            ],
        }
        pod_labels = [
            {
                "metric": {
                    "namespace": namespace,
                    "pod": app_pod,
                    "label_cabotage_io_organization": org.k8s_identifier,
                    "label_cabotage_io_project": project.k8s_identifier,
                    "label_cabotage_io_application": application.k8s_identifier,
                    "label_process": "web",
                }
            },
            {
                "metric": {
                    "namespace": namespace,
                    "pod": build_pod,
                    "label_cabotage_io_organization": org.k8s_identifier,
                    "label_cabotage_io_project": project.k8s_identifier,
                    "label_cabotage_io_application": application.k8s_identifier,
                    "label_process": "build",
                }
            },
            {
                "metric": {
                    "namespace": namespace,
                    "pod": backing_pod,
                    "label_cabotage_io_organization": org.k8s_identifier,
                    "label_cabotage_io_project": project.k8s_identifier,
                    "label_cabotage_io_environment": environment.k8s_identifier,
                }
            },
            {
                "metric": {
                    "namespace": namespace,
                    "pod": unalloc_pod,
                    "label_k8s_app": "coredns",
                }
            },
        ]

        query_range, query_instant = _mimir_stub(responses, pod_labels)
        with (
            patch("cabotage.utils.mimir.query_range", side_effect=query_range),
            patch("cabotage.utils.mimir.query_instant", side_effect=query_instant),
        ):
            run = collect_hour(PERIOD_START)

        assert run.status == "succeeded"
        items = CostLineItem.query.filter_by(run_id=run.id).all()

        app_items = [
            i
            for i in items
            if i.category == "compute" and i.application_id == application.id
        ]
        assert app_items, "app compute pod should be attributed"
        assert all(i.environment_id == environment.id for i in app_items)

        build_items = [i for i in items if i.category == "build"]
        assert build_items, "build pod should be attributed"
        assert all(i.application_id == application.id for i in build_items)
        assert all(i.environment_id == environment.id for i in build_items)

        backing_items = [i for i in items if i.category == "backing_service"]
        assert backing_items, "backing service pod should be attributed"
        assert all(i.resource_id == resource.id for i in backing_items)
        assert all(i.component == resource.slug for i in backing_items)

        # The unallocated pod produces no line items at all.
        assert run.stats["pods_unallocated"] >= 1
        assert not any(i.component == "coredns" for i in items)

    def test_overhead_pool_split_sums_to_pool(
        self, app, org, project, environment, application, app_env
    ):
        namespace = org.k8s_identifier
        app_pod = "api-web-abc12-xyz34"
        responses = {
            "rate(container_cpu_usage_seconds_total": [
                _series(namespace, app_pod, _full_hour(0.5))
            ],
            "container_memory_working_set_bytes{": [
                _series(namespace, app_pod, _full_hour(0.5 * GIB))
            ],
            'resource="cpu"': [_series(namespace, app_pod, _full_hour(1.0))],
            'resource="memory"': [_series(namespace, app_pod, _full_hour(1.0 * GIB))],
        }
        pod_labels = [
            {
                "metric": {
                    "namespace": namespace,
                    "pod": app_pod,
                    "label_cabotage_io_organization": org.k8s_identifier,
                    "label_cabotage_io_project": project.k8s_identifier,
                    "label_cabotage_io_application": application.k8s_identifier,
                    "label_process": "web",
                }
            }
        ]
        node_name = "ip-10-0-0-1.us-east-2.compute.internal"
        CostNode.query.filter_by(name=node_name).delete()
        node = CostNode(
            name=node_name,
            instance_type="m8g.xlarge",
            capacity_type="on-demand",
            cpu_capacity=4,
            memory_gib=16,
        )
        db.session.add(node)
        db.session.flush()
        node_presence = [
            {
                "metric": {"instance": "ip-10-0-0-1.us-east-2.compute.internal:10250"},
                "values": [
                    [int(PERIOD_START.timestamp()) + s * 300, "1"] for s in range(12)
                ],
            }
        ]
        query_range, query_instant = _mimir_stub(
            responses, pod_labels, node_presence=node_presence
        )
        with (
            patch("cabotage.utils.mimir.query_range", side_effect=query_range),
            patch("cabotage.utils.mimir.query_instant", side_effect=query_instant),
        ):
            run = collect_hour(PERIOD_START)

        assert run.status == "succeeded"
        overhead_items = CostLineItem.query.filter_by(
            run_id=run.id, category="overhead"
        ).all()
        assert overhead_items
        total_overhead = sum(float(i.cost_usd) for i in overhead_items)
        assert total_overhead == pytest.approx(run.stats["overhead_pool"], rel=1e-6)

        CostNode.query.filter_by(name=node_name).delete()
        db.session.commit()

    def test_idempotent_rerun_replaces_rows(
        self, app, org, project, environment, application, app_env
    ):
        namespace = org.k8s_identifier
        app_pod = "api-web-abc12-xyz34"
        responses = {
            "rate(container_cpu_usage_seconds_total": [
                _series(namespace, app_pod, _full_hour(0.5))
            ],
            "container_memory_working_set_bytes{": [
                _series(namespace, app_pod, _full_hour(0.5 * GIB))
            ],
            'resource="cpu"': [_series(namespace, app_pod, _full_hour(1.0))],
            'resource="memory"': [_series(namespace, app_pod, _full_hour(1.0 * GIB))],
        }
        pod_labels = [
            {
                "metric": {
                    "namespace": namespace,
                    "pod": app_pod,
                    "label_cabotage_io_organization": org.k8s_identifier,
                    "label_cabotage_io_project": project.k8s_identifier,
                    "label_cabotage_io_application": application.k8s_identifier,
                    "label_process": "web",
                }
            }
        ]
        query_range, query_instant = _mimir_stub(responses, pod_labels)
        with (
            patch("cabotage.utils.mimir.query_range", side_effect=query_range),
            patch("cabotage.utils.mimir.query_instant", side_effect=query_instant),
        ):
            run1 = collect_hour(PERIOD_START)
            run1_id = run1.id
            run1_period_start = run1.period_start
            count1 = CostLineItem.query.filter_by(period_start=PERIOD_START).count()
            # The hour is replaced wholesale (old run + line items deleted,
            # new ones inserted), so run1's backing row no longer exists
            # after this call; only its captured attributes remain valid.
            run2 = collect_hour(PERIOD_START)
            count2 = CostLineItem.query.filter_by(period_start=PERIOD_START).count()

        assert run1_period_start == run2.period_start
        assert CostCollectionRun.query.filter_by(period_start=PERIOD_START).count() == 1
        assert count1 == count2
        assert run1_id != run2.id

    def test_failed_mimir_query_produces_failed_run_with_no_items(
        self, app, org, project, environment, application, app_env
    ):
        query_range, query_instant = _mimir_stub(
            {}, [], raise_on="rate(container_cpu_usage_seconds_total"
        )
        with (
            patch("cabotage.utils.mimir.query_range", side_effect=query_range),
            patch("cabotage.utils.mimir.query_instant", side_effect=query_instant),
        ):
            run = collect_hour(PERIOD_START)

        assert run.status == "failed"
        assert run.error
        assert CostLineItem.query.filter_by(run_id=run.id).count() == 0

    def test_zero_pod_metrics_produces_failed_run(
        self, app, org, project, environment, application, app_env
    ):
        query_range, query_instant = _mimir_stub({}, [])
        with (
            patch("cabotage.utils.mimir.query_range", side_effect=query_range),
            patch("cabotage.utils.mimir.query_instant", side_effect=query_instant),
        ):
            run = collect_hour(PERIOD_START)

        assert run.status == "failed"
        assert run.error == "no pod metrics for hour"
        assert CostLineItem.query.filter_by(run_id=run.id).count() == 0

    def test_good_hour_survives_failed_recollection(
        self, app, org, project, environment, application, app_env
    ):
        namespace = org.k8s_identifier
        cpu_usage = _series(namespace, "api-web-full", _full_hour(0.5))
        cpu_request = _series(namespace, "api-web-full", _full_hour(1.0))
        mem_usage = _series(namespace, "api-web-full", _full_hour(0.8 * GIB))
        mem_request = _series(namespace, "api-web-full", _full_hour(1.0 * GIB))
        pod_labels = [
            {
                "metric": {
                    "namespace": namespace,
                    "pod": "api-web-full",
                    "label_cabotage_io_organization": org.k8s_identifier,
                    "label_cabotage_io_project": project.k8s_identifier,
                    "label_cabotage_io_application": application.k8s_identifier,
                    "label_process": "web",
                }
            }
        ]
        responses = {
            "rate(container_cpu_usage_seconds_total": [cpu_usage],
            "container_memory_working_set_bytes{": [mem_usage],
            'resource="cpu"': [cpu_request],
            'resource="memory"': [mem_request],
        }
        good_query_range, good_query_instant = _mimir_stub(responses, pod_labels)
        with (
            patch("cabotage.utils.mimir.query_range", side_effect=good_query_range),
            patch("cabotage.utils.mimir.query_instant", side_effect=good_query_instant),
        ):
            good_run = collect_hour(PERIOD_START)

        assert good_run.status == "succeeded"
        assert good_run.stats["last_attempt_status"] == "succeeded"
        good_run_id = good_run.id
        good_item_count = CostLineItem.query.filter_by(run_id=good_run_id).count()
        assert good_item_count > 0

        # A Mimir outage on re-collection must never clobber the existing
        # succeeded run with an empty replace, but the CLI/freshness still
        # need to see that THIS attempt failed, not just whatever the kept
        # run's own status says.
        empty_query_range, empty_query_instant = _mimir_stub({}, [])
        with (
            patch("cabotage.utils.mimir.query_range", side_effect=empty_query_range),
            patch(
                "cabotage.utils.mimir.query_instant", side_effect=empty_query_instant
            ),
        ):
            recollected = collect_hour(PERIOD_START)

        assert recollected.id == good_run_id
        assert recollected.status == "succeeded"
        assert recollected.stats["last_attempt_status"] == "failed"
        assert recollected.stats["last_attempt_error"] == "no pod metrics for hour"
        assert recollected.stats["last_attempt_at"]
        assert (
            CostLineItem.query.filter_by(run_id=good_run_id).count() == good_item_count
        )
        assert CostCollectionRun.query.filter_by(period_start=PERIOD_START).count() == 1

    def test_missing_pod_labels_with_usage_produces_failed_run(
        self, app, org, project, environment, application, app_env
    ):
        # cAdvisor usage present but kube-state-metrics labels missing must
        # not be recorded as a succeeded hour with every pod unallocated.
        responses = _app_pod_responses(org.k8s_identifier, "api-web-abc12-xyz34")
        run = _collect_with(responses, [])

        assert run.status == "failed"
        assert run.error == "no pod labels for hour"
        assert CostLineItem.query.filter_by(run_id=run.id).count() == 0

    def test_redis_storage_counts_replicas_only_when_ha(
        self, app, org, project, environment, application, app_env
    ):
        common = {
            "environment_id": environment.id,
            "service_version": "8",
            "size_class": "cache.medium",
            "storage_size": 10,
            "leader_replicas": 3,
            "follower_replicas": 3,
        }
        standalone = RedisResource(name="cache", ha_enabled=False, **common)
        cluster = RedisResource(name="cluster", ha_enabled=True, **common)
        for resource in (standalone, cluster):
            # Storage only counts resources that existed during the hour.
            resource.created = PERIOD_START.replace(tzinfo=None, hour=0)
        db.session.add_all([standalone, cluster])
        db.session.flush()

        pod = "api-web-abc12-xyz34"
        run = _collect_with(
            _app_pod_responses(org.k8s_identifier, pod),
            _app_pod_labels(org, project, application, pod),
        )

        assert run.status == "succeeded"
        storage = {
            i.resource_id: i.quantity
            for i in CostLineItem.query.filter_by(run_id=run.id, category="storage")
        }
        assert storage[standalone.id] == pytest.approx(10 / 730.0)
        assert storage[cluster.id] == pytest.approx(60 / 730.0)

    def test_long_resource_slug_does_not_fail_the_run(
        self, app, org, project, environment, application, app_env
    ):
        long_slug = "a" * 80
        resource = PostgresResource(
            environment_id=environment.id,
            name="long",
            slug=long_slug,
            service_version="18",
            size_class="db.medium",
            storage_size=10,
        )
        resource.created = PERIOD_START.replace(tzinfo=None, hour=0)
        db.session.add(resource)
        db.session.flush()

        pod = "api-web-abc12-xyz34"
        run = _collect_with(
            _app_pod_responses(org.k8s_identifier, pod),
            _app_pod_labels(org, project, application, pod),
        )

        assert run.status == "succeeded"
        item = CostLineItem.query.filter_by(
            run_id=run.id, resource_id=resource.id, category="storage"
        ).one()
        assert item.component == long_slug[:64]


class TestCollectCostsBackoff:
    """collect_costs' failed-hour retry backoff (cabotage.celery.tasks.cost)."""

    LAST_HOUR = datetime.datetime(2026, 6, 1, 12, 0, 0, tzinfo=datetime.UTC)

    def _make_failed_run(self, period_start, last_attempt_at):
        run = CostCollectionRun(
            period_start=period_start,
            status="failed",
            started_at=period_start,
            error="no pod metrics for hour",
            stats={
                "last_attempt_status": "failed",
                "last_attempt_error": "no pod metrics for hour",
                "last_attempt_at": last_attempt_at.isoformat(),
            },
        )
        db.session.add(run)
        db.session.commit()
        return run

    def _run_collect_costs(self):
        with (
            patch(
                "cabotage.celery.tasks.cost._last_completed_hour",
                return_value=self.LAST_HOUR,
            ),
            patch("cabotage.celery.tasks.cost.collect_hour") as mock_collect_hour,
        ):
            mock_collect_hour.side_effect = lambda hour: CostCollectionRun(
                period_start=hour,
                status="succeeded",
                started_at=hour,
                stats={"last_attempt_status": "succeeded", "last_attempt_error": None},
            )
            collect_costs()
            collected_hours = {
                call.args[0] for call in mock_collect_hour.call_args_list
            }
        return collected_hours

    def test_recently_failed_hour_is_skipped(self, app):
        hour = self.LAST_HOUR - datetime.timedelta(hours=3)
        self._make_failed_run(hour, self.LAST_HOUR - (FAILED_HOUR_RETRY_AFTER / 2))
        try:
            collected_hours = self._run_collect_costs()
        finally:
            CostCollectionRun.query.filter_by(period_start=hour).delete()
            db.session.commit()

        assert hour not in collected_hours

    def test_old_failed_hour_is_retried(self, app):
        hour = self.LAST_HOUR - datetime.timedelta(hours=3)
        self._make_failed_run(
            hour, self.LAST_HOUR - FAILED_HOUR_RETRY_AFTER - datetime.timedelta(hours=1)
        )
        try:
            collected_hours = self._run_collect_costs()
        finally:
            CostCollectionRun.query.filter_by(period_start=hour).delete()
            db.session.commit()

        assert hour in collected_hours

    def test_never_attempted_and_most_recent_hour_always_collected(self, app):
        never_attempted = self.LAST_HOUR - datetime.timedelta(hours=10)
        CostCollectionRun.query.filter_by(period_start=never_attempted).delete()
        db.session.commit()

        try:
            collected_hours = self._run_collect_costs()
        finally:
            CostCollectionRun.query.filter(
                CostCollectionRun.period_start.in_([never_attempted, self.LAST_HOUR])
            ).delete()
            db.session.commit()

        assert never_attempted in collected_hours
        assert self.LAST_HOUR in collected_hours
