"""Rolling back to a previous deployment's release."""

import datetime
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from flask_security import hash_password

from cabotage.celery.tasks.notify import _dispatch_pipeline_notification_impl
from cabotage.celery.tasks.prune_images import (
    _prune_repository,
    _rollback_release_aliases,
)
from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Configuration,
    Deployment,
    Environment,
    Image,
    Project,
    Release,
)
from cabotage.server.rollback import plan_rollback
from cabotage.server.wsgi import app as _app

T0 = datetime.datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture
def ctx(monkeypatch):
    for key, value in {
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "REQUIRE_MFA": False,
        "KUBERNETES_ENABLED": True,
    }.items():
        monkeypatch.setitem(_app.config, key, value)
    with _app.app_context(), patch.object(db.session, "commit", db.session.flush):
        try:
            yield _setup()
        finally:
            db.session.rollback()


def _setup():
    user = User(
        username=f"rollbacker-{uuid.uuid4().hex[:8]}",
        email=f"rb-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    org = Organization(name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
    db.session.add_all([user, org])
    db.session.flush()
    db.session.add(
        OrganizationMember(organization_id=org.id, user_id=user.id, admin=True)
    )
    project = Project(name="Proj", organization_id=org.id)
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
    base = f"/projects/{org.slug}/{project.slug}/applications/{application.slug}"
    return client, user, application, app_env, base


def _config(application, app_env, name):
    cfg = Configuration(
        application_id=application.id,
        application_environment_id=app_env.id,
        name=name,
        value="v",
    )
    db.session.add(cfg)
    db.session.flush()
    return cfg


def _release(application, app_env, configs, processes):
    repo = Image._build_repository_name(
        application.project.organization.k8s_identifier,
        application.project.k8s_identifier,
        application.k8s_identifier,
        None,
    )
    image = Image(
        application_id=application.id,
        application_environment_id=app_env.id,
        _repository_name=repo,
        build_ref="main",
        built=True,
        processes=processes,
        image_metadata={"sha": uuid.uuid4().hex},
    )
    db.session.add(image)
    db.session.flush()
    release = Release(
        application_id=application.id,
        application_environment_id=app_env.id,
        _repository_name=repo,
        image=image.asdict,
        configuration={c.name: c.asdict for c in configs},
        image_changes={},
        configuration_changes={},
        platform=application.platform,
        built=True,
    )
    db.session.add(release)
    db.session.flush()
    return release


def _deployment(application, app_env, release, minutes, *, error=False):
    dep = Deployment(
        application_id=application.id,
        application_environment_id=app_env.id,
        release=release.asdict,
        complete=not error,
        error=error,
        created=T0 + datetime.timedelta(minutes=minutes),
        updated=T0 + datetime.timedelta(minutes=minutes),
    )
    db.session.add(dep)
    db.session.flush()
    return dep


def _proc(cmd):
    return {"cmd": cmd, "env": []}


@pytest.fixture
def history(ctx):
    """v1 (old, good) -> v2 (live).

    v2 rotated SECRET_KEY, added DEBUG and a worker process. PORT is in both,
    unchanged; it is edited after v2 went live.
    """
    client, user, application, app_env, base = ctx
    secret = _config(application, app_env, "SECRET_KEY")
    port = _config(application, app_env, "PORT")
    v1 = _release(
        application,
        app_env,
        [secret, port],
        {"web": _proc("gunicorn"), "release": _proc("migrate")},
    )
    secret.value = "rotated"
    db.session.flush()  # bumps version_id
    debug = _config(application, app_env, "DEBUG")
    v2 = _release(
        application,
        app_env,
        [secret, port, debug],
        {"web": _proc("gunicorn"), "worker": _proc("celery")},
    )
    port.value = "9000"
    db.session.flush()  # edited after v2; not part of any release
    old = _deployment(application, app_env, v1, 0)
    live = _deployment(application, app_env, v2, 10)
    return client, user, app_env, base, secret, v1, v2, old, live


def _deployments(app_env):
    return app_env.deployments.order_by(Deployment.created.desc()).all()


def test_plan_compares_target_release_with_live_release(history):
    _client, _user, _app_env, _base, _secret, _v1, _v2, old, live = history

    with patch("cabotage.server.rollback._release_tag_exists", return_value=True):
        plan = plan_rollback(old)

    assert plan.allowed
    assert plan.current == live
    assert plan.config_removed == ["DEBUG"]
    assert plan.config_added == []
    assert plan.config_changed == ["SECRET_KEY"]  # PORT's later edit is in neither
    assert plan.release_commands == ["release"]


def test_confirm_page_renders_plan(history):
    client, _user, _app_env, base, _secret, v1, _v2, old, _live = history

    with patch("cabotage.server.rollback._release_tag_exists", return_value=True):
        resp = client.get(f"{base}/deployments/{old.id}/rollback")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert f"Roll back to v{v1.version}" in html
    assert f"Reverts to v{v1.version} value" in html
    assert "Can't roll back" not in html


def test_rollback_deploys_target_release(history):
    client, user, app_env, base, _secret, v1, v2, old, live = history

    with (
        patch("cabotage.server.rollback._release_tag_exists", return_value=True),
        patch("cabotage.server.user.views.run_deploy") as run_deploy,
        patch("cabotage.server.user.views.dispatch_pipeline_notification") as notify,
    ):
        resp = client.post(f"{base}/deployments/{old.id}/rollback")

    assert resp.status_code == 302
    new = _deployments(app_env)[0]
    assert new.id not in (old.id, live.id)
    assert new.release["id"] == str(v1.id)
    assert new.trigger_type == "rollback"
    assert new.deploy_metadata == {
        "trigger": "rollback",
        "triggered_by": user.username,
        "rollback_to_version": v1.version,
        "rollback_from_version": v2.version,
    }
    run_deploy.delay.assert_called_once_with(deployment_id=new.id)
    assert notify.delay.call_args.kwargs["detail"] == (
        f"Rollback v{v2.version} → v{v1.version} by {user.username}"
    )


@pytest.mark.parametrize(
    "case",
    ["live", "same_release", "failed", "pruned", "config_deleted"],
)
def test_rollback_blocked(history, case):
    client, _user, app_env, base, secret, v1, v2, old, live = history
    application = old.application
    target = old
    tag_exists = True
    if case == "live":
        target = live
    elif case == "same_release":
        target = _deployment(application, app_env, v2, 5)
    elif case == "failed":
        target = _deployment(application, app_env, v1, -5, error=True)
    elif case == "pruned":
        tag_exists = False
    elif case == "config_deleted":
        db.session.delete(secret)
        db.session.flush()
    before = len(_deployments(app_env))

    with (
        patch("cabotage.server.rollback._release_tag_exists", return_value=tag_exists),
        patch("cabotage.server.user.views.run_deploy") as run_deploy,
    ):
        resp = client.post(f"{base}/deployments/{target.id}/rollback")

    assert resp.status_code == 409
    assert "Can't roll back" in resp.get_data(as_text=True)
    run_deploy.delay.assert_not_called()
    assert len(_deployments(app_env)) == before


@pytest.mark.parametrize("deleted", ["app_env", "environment"])
def test_rollback_rejected_after_environment_deleted(history, deleted):
    client, _user, app_env, base, _secret, _v1, _v2, old, _live = history
    target = app_env if deleted == "app_env" else app_env.environment
    target.deleted_at = T0
    db.session.flush()
    before = len(_deployments(app_env))

    with (
        patch("cabotage.server.rollback._release_tag_exists", return_value=True),
        patch("cabotage.server.user.views.run_deploy") as run_deploy,
    ):
        detail = client.get(f"{base}/deployments/{old.id}")
        page = client.get(f"{base}/deployments/{old.id}/rollback")
        submit = client.post(f"{base}/deployments/{old.id}/rollback")

    assert detail.status_code == 200
    assert "Roll back to this" not in detail.get_data(as_text=True)
    assert page.status_code == 404
    assert submit.status_code == 404
    run_deploy.delay.assert_not_called()
    assert len(_deployments(app_env)) == before


def test_completion_notification_keeps_rollback_context(history):
    _client, user, app_env, _base, _secret, v1, v2, old, _live = history
    rollback = _deployment(old.application, app_env, v1, 20)
    rollback.deploy_metadata = {
        "trigger": "rollback",
        "triggered_by": user.username,
        "rollback_to_version": v1.version,
        "rollback_from_version": v2.version,
    }
    db.session.flush()
    org = old.application.project.organization

    with (
        patch(
            "cabotage.celery.tasks.notify.resolve_routes",
            return_value=[("slack", "C1")],
        ),
        patch("cabotage.celery.tasks.notify.send_notification") as send,
    ):
        _dispatch_pipeline_notification_impl(
            "pipeline.deploy",
            "Deployment",
            str(rollback.id),
            str(org.id),
            str(old.application.id),
            str(app_env.id),
            complete=True,
        )

    message = send.delay.call_args.args[-1]
    assert f"Rollback v{v2.version} → v{v1.version} by {user.username}" in str(message)


def test_prune_keeps_recently_deployed_release_tags(history):
    _client, _user, app_env, _base, _secret, v1, v2, _old, _live = history
    failed = _release(app_env.application, app_env, [], {})
    _deployment(app_env.application, app_env, failed, 30, error=True)

    retained = _rollback_release_aliases(app_env)
    assert retained == {f"release-{v1.version}", f"release-{v2.version}"}

    client = MagicMock()
    client.list_aliases.return_value = [f"release-{n}" for n in range(1, 13)]
    with patch("cabotage.celery.tasks.prune_images.DXF", return_value=client):
        _prune_repository("cabotage/o/p/a", retain=frozenset({"release-2"}))

    deleted = {c.args[0] for c in client.del_alias.call_args_list}
    assert deleted == {f"release-{n}" for n in (1, 3, 4, 5, 6, 7)}
