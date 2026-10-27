"""Tests for the synthetic cost history seed (cabotage.server.applets.cost.demo)."""

import datetime
import uuid

import pytest

from cabotage.server import db
from cabotage.server.applets.cost.demo import seed_cost_history
from cabotage.server.models.auth import Organization
from cabotage.server.models.cost import CostCollectionRun, CostLineItem
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Project,
)
from cabotage.server.models.resources import PostgresResource, RedisResource, Resource
from cabotage.server.wsgi import app as _app

NOW = datetime.datetime(2025, 3, 10, 12, 0, tzinfo=datetime.UTC)
DAYS = 4


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    with _app.app_context():
        yield _app


@pytest.fixture(autouse=True)
def _clean_runs(app):
    def _delete():
        CostCollectionRun.query.filter(
            CostCollectionRun.period_start >= NOW - datetime.timedelta(days=30),
            CostCollectionRun.period_start <= NOW,
        ).delete()
        db.session.commit()

    _delete()
    yield
    _delete()


@pytest.fixture
def org(app):
    organization = Organization(
        name="Demo Org", slug=f"demo-org-{uuid.uuid4().hex[:8]}"
    )
    db.session.add(organization)
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
            ApplicationEnvironment.query.filter_by(
                application_id=application.id
            ).delete()
        db.session.flush()
        Application.query.filter_by(project_id=project.id).delete()
        Environment.query.filter_by(project_id=project.id).delete()
    Project.query.filter_by(organization_id=organization.id).delete()
    db.session.flush()
    db.session.delete(organization)
    db.session.commit()


@pytest.fixture
def environment(org):
    project = Project(name="Demo Project", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    env = Environment(name="production", project_id=project.id)
    db.session.add(env)
    db.session.commit()
    return env


def _add_apps(environment, replicas):
    apps = []
    for i, count in enumerate(replicas):
        application = Application(
            name=f"App {i}", slug=f"app-{i}", project_id=environment.project_id
        )
        db.session.add(application)
        db.session.flush()
        db.session.add(
            ApplicationEnvironment(
                application_id=application.id,
                environment_id=environment.id,
                k8s_identifier=environment.k8s_identifier,
                process_counts={"web": count},
            )
        )
        apps.append(application)
    db.session.commit()
    return apps


def test_reseed_keeps_real_line_items_older_than_window(org, environment):
    old_hour = NOW - datetime.timedelta(days=DAYS + 5)
    real_run = CostCollectionRun(
        period_start=old_hour, status="succeeded", started_at=old_hour, stats={}
    )
    db.session.add(real_run)
    db.session.flush()
    real_item = CostLineItem(
        run_id=real_run.id,
        period_start=old_hour,
        organization_id=org.id,
        project_id=environment.project_id,
        category="compute",
        meter="vcpu",
        component="web",
        quantity=1.0,
        unit="vCPU-hr",
        cost_usd=0.04,
    )
    db.session.add(real_item)
    db.session.commit()
    real_item_id = real_item.id

    seed_cost_history(org, days=DAYS, now=NOW)

    assert db.session.get(CostLineItem, real_item_id) is not None


def test_reseed_decommissions_one_app_and_keeps_its_history(org, environment):
    apps = _add_apps(environment, [1, 2, 3, 4])
    app_ids = [a.id for a in apps]

    seed_cost_history(org, days=DAYS, now=NOW)
    seed_cost_history(org, days=DAYS, now=NOW)

    deleted = [
        a
        for a in Application.query.filter(Application.id.in_(app_ids)).all()
        if a.deleted_at is not None
    ]
    assert len(deleted) == 1
    hours = [
        row.period_start
        for row in CostLineItem.query.filter_by(application_id=deleted[0].id).all()
    ]
    assert hours
    assert max(hours) < deleted[0].deleted_at.replace(tzinfo=datetime.UTC)


def test_backing_service_compute_uses_size_class_requests_per_instance(
    org, environment
):
    postgres = PostgresResource(
        environment_id=environment.id,
        name="maindb",
        service_version="18",
        size_class="db.2xlarge",
        storage_size=10,
        ha_enabled=True,
    )
    redis = RedisResource(
        environment_id=environment.id,
        name="cache",
        service_version="8",
        size_class="cache.nano",
        storage_size=1,
        ha_enabled=False,
    )
    db.session.add_all([postgres, redis])
    db.session.commit()

    seed_cost_history(org, days=DAYS, now=NOW)

    def _requests(resource):
        rows = CostLineItem.query.filter_by(
            resource_id=resource.id, category="backing_service"
        ).all()
        return {row.meter: row.request_quantity for row in rows}

    assert _requests(postgres) == {"vcpu": 8.0, "memory": 16.0}
    assert _requests(redis) == pytest.approx({"vcpu": 0.033, "memory": 0.0625})
