"""Regression tests for the pipeline stats/runs endpoints."""

import datetime
import time
import uuid
from unittest.mock import patch

import pytest
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Deployment,
    Environment,
    Image,
    Project,
    Release,
)
from cabotage.server.wsgi import app as _app


@pytest.fixture
def pipeline_app(monkeypatch):
    for key, value in {
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "REQUIRE_MFA": False,
    }.items():
        monkeypatch.setitem(_app.config, key, value)
    with _app.app_context(), patch.object(db.session, "commit", db.session.flush):
        try:
            yield _pipeline_app()
        finally:
            db.session.rollback()


def _pipeline_app():
    user = User(
        username=f"testadmin-{uuid.uuid4().hex[:8]}",
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    org = Organization(name="Test Org", slug=f"testorg-{uuid.uuid4().hex[:8]}")
    db.session.add_all([user, org])
    db.session.flush()
    db.session.add(
        OrganizationMember(organization_id=org.id, user_id=user.id, admin=True)
    )
    project = Project(name="My Project", organization_id=org.id)
    db.session.add(project)
    db.session.flush()
    environment = Environment(name="default", project_id=project.id, ephemeral=False)
    db.session.add(environment)
    db.session.flush()
    application = Application(name="web", slug="web", project_id=project.id)
    db.session.add(application)
    db.session.flush()
    app_env = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
        k8s_identifier=None,
    )
    db.session.add(app_env)
    db.session.flush()
    client = _app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"

    base = (
        f"/projects/{org.slug}/{project.slug}/applications/{application.slug}/pipeline"
    )
    return client, application, app_env, base


def _repo_name(application, app_env):
    return Image._build_repository_name(
        application.project.organization.k8s_identifier,
        application.project.k8s_identifier,
        application.k8s_identifier,
        app_env.k8s_identifier,
    )


def _make_image(application, app_env, created, updated, *, built=True, error=False):
    img = Image(
        application_id=application.id,
        application_environment_id=app_env.id,
        _repository_name=_repo_name(application, app_env),
        build_ref="main",
        built=built,
        error=error,
        processes={},
        image_metadata={"sha": "deadbeef"},
        created=created,
        updated=updated,
    )
    db.session.add(img)
    db.session.flush()
    return img


def _make_release(
    application, app_env, image, created, updated, *, built=True, error=False
):
    release = Release(
        application_id=application.id,
        application_environment_id=app_env.id,
        _repository_name=_repo_name(application, app_env),
        image=image.asdict,
        configuration={},
        image_changes={"added": [], "removed": [], "changed": [], "unchanged": []},
        configuration_changes={
            "added": [],
            "removed": [],
            "changed": [],
            "unchanged": [],
        },
        platform=application.platform,
        built=built,
        error=error,
        created=created,
        updated=updated,
    )
    db.session.add(release)
    db.session.flush()
    return release


def _make_deployment(
    application,
    app_env,
    release,
    created,
    updated,
    *,
    complete=True,
    error=False,
    deploy_metadata=None,
):
    dep = Deployment(
        application_id=application.id,
        application_environment_id=app_env.id,
        release=release.asdict,
        complete=complete,
        error=error,
        deploy_metadata=deploy_metadata,
        created=created,
        updated=updated,
    )
    db.session.add(dep)
    db.session.flush()
    return dep


def _iso_day(dt):
    return dt.date().isoformat()


def test_pipeline_window_rejects_overflowing_until(pipeline_app):
    client, _application, _app_env, base = pipeline_app
    response = client.get(f"{base}/stats?since=2024-01-01&until=9999-12-31")
    assert response.status_code == 400


def test_pipeline_window_accepts_valid_reversed_bounds(pipeline_app):
    client, application, app_env, base = pipeline_app
    start = datetime.datetime(2000, 1, 1)
    img = _make_image(application, app_env, start, start)
    rel = _make_release(application, app_env, img, start, start)
    for finished in (
        start,
        datetime.datetime(2000, 1, 5, 23, 59, 59),
        datetime.datetime(2000, 1, 6),
    ):
        _make_deployment(
            application,
            app_env,
            rel,
            finished - datetime.timedelta(seconds=1),
            finished,
        )
    response = client.get(f"{base}/stats?since=2000-01-05&until=2000-01-01")
    assert response.status_code == 200
    assert response.get_json()["summary"]["runs"] == 2


@pytest.mark.parametrize("offset,status_code", [(2**63 - 1, 200), (2**63, 400)])
def test_pipeline_runs_rejects_oversized_offset(pipeline_app, offset, status_code):
    client, _application, _app_env, base = pipeline_app
    response = client.get(f"{base}/runs?offset={offset}")
    assert response.status_code == status_code


def test_pipeline_runs_clamps_negative_offset(pipeline_app):
    client, application, app_env, base = pipeline_app
    start = datetime.datetime(2000, 1, 1, 12)
    img = _make_image(application, app_env, start, start)
    rel = _make_release(application, app_env, img, start, start)
    dep = _make_deployment(
        application,
        app_env,
        rel,
        start,
        start + datetime.timedelta(seconds=1),
    )
    response = client.get(f"{base}/runs?since=2000-01-01&until=2000-01-01&offset=-5")
    assert response.status_code == 200
    assert [run["id"] for run in response.get_json()["runs"]] == [str(dep.id)]


def test_deploy_only_redeploy_excludes_historical_build_time(pipeline_app):
    client, application, app_env, base = pipeline_app
    now = datetime.datetime(2000, 1, 10, 12)

    # Historical build, long before the redeploy.
    old = now - datetime.timedelta(days=5)
    img = _make_image(application, app_env, old, old + datetime.timedelta(seconds=100))
    rel = _make_release(
        application,
        app_env,
        img,
        old + datetime.timedelta(seconds=100),
        old + datetime.timedelta(seconds=120),
    )

    # A plain redeploy/rollback reuses the release and only runs a deploy.
    dep_start = now - datetime.timedelta(minutes=10)
    dep_end = dep_start + datetime.timedelta(seconds=30)
    _make_deployment(
        application,
        app_env,
        rel,
        dep_start,
        dep_end,
        deploy_metadata={"trigger": "manual_deploy", "triggered_by": "alice"},
    )

    day = _iso_day(now)
    response = client.get(f"{base}/runs?since={day}&until={day}")
    assert response.status_code == 200
    runs = response.get_json()["runs"]
    assert len(runs) == 1
    run = runs[0]
    assert run["image_duration"] is None
    assert run["release_duration"] is None
    assert run["total_duration"] == pytest.approx(30.0)
    assert run["started"] == dep_start.isoformat() + "Z"


def test_manual_build_and_deploy_counts_full_pipeline(pipeline_app):
    client, application, app_env, base = pipeline_app
    now = datetime.datetime(2000, 1, 10, 12)
    t0 = now - datetime.timedelta(minutes=30)

    img = _make_image(application, app_env, t0, t0 + datetime.timedelta(seconds=100))
    rel = _make_release(
        application,
        app_env,
        img,
        t0 + datetime.timedelta(seconds=100),
        t0 + datetime.timedelta(seconds=120),
    )
    # Manual "Build and deploy" sets both auto_deploy and trigger=manual_deploy.
    _make_deployment(
        application,
        app_env,
        rel,
        t0 + datetime.timedelta(seconds=120),
        t0 + datetime.timedelta(seconds=150),
        deploy_metadata={
            "auto_deploy": True,
            "trigger": "manual_deploy",
            "triggered_by": "alice",
        },
    )

    day = _iso_day(now)
    response = client.get(f"{base}/runs?since={day}&until={day}")
    assert response.status_code == 200
    run = response.get_json()["runs"][0]
    assert run["image_duration"] == pytest.approx(100.0)
    assert run["release_duration"] == pytest.approx(20.0)
    assert run["deploy_duration"] == pytest.approx(30.0)
    assert run["total_duration"] == pytest.approx(150.0)

    stats = client.get(f"{base}/stats?since={day}&until={day}").get_json()
    assert stats["summary"]["run_p50"] == pytest.approx(150.0)


def test_overlapping_image_release_build_time_counted_once(pipeline_app):
    client, application, app_env, base = pipeline_app
    now = datetime.datetime(2000, 1, 10, 12)
    t0 = now - datetime.timedelta(minutes=30)

    # Image and release build windows fully overlap (e.g. an omnibus build
    # that reports both as happening over the same wall-clock span).
    img = _make_image(application, app_env, t0, t0 + datetime.timedelta(seconds=120))
    rel = _make_release(
        application, app_env, img, t0, t0 + datetime.timedelta(seconds=120)
    )
    _make_deployment(
        application,
        app_env,
        rel,
        t0 + datetime.timedelta(seconds=120),
        t0 + datetime.timedelta(seconds=150),
        deploy_metadata={"auto_deploy": True},
    )

    day = _iso_day(now)
    response = client.get(f"{base}/runs?since={day}&until={day}")
    assert response.status_code == 200
    run = response.get_json()["runs"][0]
    assert run["image_duration"] == pytest.approx(120.0)
    assert run["release_duration"] == pytest.approx(120.0)
    assert run["deploy_duration"] == pytest.approx(30.0)
    # 120 + 120 + 30 minus the full 120s overlap between image and release.
    assert run["total_duration"] == pytest.approx(150.0)

    stats = client.get(f"{base}/stats?since={day}&until={day}").get_json()
    assert stats["summary"]["run_p50"] == pytest.approx(150.0)


def test_running_pipeline_has_no_total(pipeline_app):
    client, application, app_env, base = pipeline_app
    start = datetime.datetime(2000, 1, 10, 12)
    img = _make_image(
        application, app_env, start, start + datetime.timedelta(seconds=100)
    )
    rel = _make_release(
        application,
        app_env,
        img,
        start + datetime.timedelta(seconds=100),
        start + datetime.timedelta(seconds=120),
    )
    _make_deployment(
        application,
        app_env,
        rel,
        start + datetime.timedelta(seconds=120),
        start + datetime.timedelta(seconds=150),
        complete=False,
        deploy_metadata={"auto_deploy": True},
    )
    response = client.get(f"{base}/runs?since=2000-01-10&until=2000-01-10")
    assert response.status_code == 200
    run = response.get_json()["runs"][0]
    assert run["deploy_status"] == "pending"
    assert run["deploy_duration"] is None
    assert run["total_duration"] is None


@pytest.mark.parametrize(
    "metadata,release_start,expected",
    [
        ({"trigger": "manual_deploy"}, 120, 30.0),
        ({"auto_deploy": True}, 120, 270.0),
        ({"auto_deploy": True}, 0, 150.0),
    ],
)
def test_rollup_run_time_matches_deployment_type(
    pipeline_app, metadata, release_start, expected
):
    client, application, app_env, base = pipeline_app
    start = datetime.datetime(2000, 1, 10, 12)
    img = _make_image(
        application, app_env, start, start + datetime.timedelta(seconds=120)
    )
    rel = _make_release(
        application,
        app_env,
        img,
        start + datetime.timedelta(seconds=release_start),
        start + datetime.timedelta(seconds=release_start + 120),
    )
    _make_deployment(
        application,
        app_env,
        rel,
        start + datetime.timedelta(seconds=240),
        start + datetime.timedelta(seconds=270),
        deploy_metadata=metadata,
    )
    window = "?since=2000-01-10&until=2000-01-10"
    stats = client.get(f"{base}/stats{window}").get_json()
    response = client.get(
        f"/organizations/{application.project.organization.slug}/pipeline/rollup{window}"
    )
    assert response.status_code == 200
    rollup = response.get_json()
    row = next(row for row in rollup["rows"] if row["app_env_id"] == str(app_env.id))
    for percentile in ("p50", "p95"):
        assert row[percentile] == pytest.approx(expected)
        assert rollup["summary"][f"run_{percentile}"] == pytest.approx(expected)
        assert stats["summary"][f"run_{percentile}"] == pytest.approx(expected)


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {"trigger": "manual_deploy"},
        {"auto_deploy": False, "trigger": "rollback"},
        {"auto_deploy": True},
    ],
)
def test_live_run_only_includes_current_pipeline_stages(pipeline_app, metadata):
    client, application, app_env, base = pipeline_app
    start = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    img = _make_image(
        application,
        app_env,
        start - datetime.timedelta(minutes=2),
        start - datetime.timedelta(minutes=1),
    )
    rel = _make_release(
        application,
        app_env,
        img,
        start - datetime.timedelta(minutes=1),
        start - datetime.timedelta(seconds=30),
    )
    dep = _make_deployment(
        application,
        app_env,
        rel,
        start,
        start,
        complete=False,
        deploy_metadata=metadata,
    )
    response = client.get(f"{base}/live")
    assert response.status_code == 200
    run = response.get_json()["run"]
    assert run["status"] == "running"
    assert run["deploy"]["id"] == str(dep.id)
    assert run["sha"] == "deadbee"
    if (metadata or {}).get("auto_deploy"):
        assert run["image"]["id"] == str(img.id)
        assert run["release"]["id"] == str(rel.id)
        assert run["started"] == img.created.isoformat() + "Z"
    else:
        assert run["image"] is None
        assert run["release"] is None
        assert run["started"] == start.isoformat() + "Z"


def test_overview_processes_follow_deployed_image(pipeline_app):
    client, application, app_env, base = pipeline_app
    start = datetime.datetime(2000, 1, 10, 12)
    deployed_img = _make_image(application, app_env, start, start)
    deployed_img.processes = {"web": {"cmd": "gunicorn", "env": []}}
    deployed_rel = _make_release(application, app_env, deployed_img, start, start)
    _make_deployment(application, app_env, deployed_rel, start, start)
    later = start + datetime.timedelta(hours=1)
    next_img = _make_image(application, app_env, later, later)
    next_img.processes = {"queue-consumer": {"cmd": "celery", "env": []}}
    _make_release(application, app_env, next_img, later, later, built=False)
    db.session.flush()

    response = client.get(base.removesuffix("/pipeline"))
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '<code class="text-xs text-accent font-semibold">web</code>' in html
    assert "queue-consumer" not in html
