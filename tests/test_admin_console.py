import datetime
import uuid
from collections.abc import Iterator
from typing import cast

import pytest
from flask import Flask, url_for
from flask.testing import FlaskClient
from sqlalchemy import select

from cabotage.server import db
from cabotage.server.admin_console import queries
from cabotage.server.models.audit import AuditLog
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Configuration,
    Deployment,
    Environment,
    EnvironmentConfiguration,
    Project,
    activity_plugin,
)
from tests.admin_passkey_helpers import SigningPasskey
from tests.test_organization_requests import (
    _login,
    admin_user as admin_user,
    app as app,
    client as client,
    regular_user as regular_user,
)


def test_role_change_requires_elevation_and_revokes_it(
    app: Flask, client: FlaskClient, admin_user: User, regular_user: User
) -> None:
    actor_key = SigningPasskey.register(admin_user)
    _login(client, admin_user)
    actor_key.enter(client)
    target_client = app.test_client()
    _login(target_client, regular_user)
    assert target_client.get("/admin/").status_code == 403

    url = f"/admin/users/{regular_user.id}/admin"
    response = actor_key.post(client, url, {"admin": "true"})
    assert response.status_code == 302
    db.session.refresh(regular_user)
    assert regular_user.admin is True
    # Promotion alone does not turn an ordinary login into an elevated session.
    assert target_client.get("/admin/").status_code == 302
    target_key = SigningPasskey.register(regular_user)
    target_key.enter(target_client)
    assert target_client.get("/admin/").status_code == 200

    response = actor_key.post(client, url, {"admin": "false"})
    assert response.status_code == 302
    db.session.refresh(regular_user)
    assert regular_user.admin is False
    assert target_client.get("/admin/").status_code == 403


def test_reactivation_does_not_restore_revoked_login(
    app: Flask, client: FlaskClient, admin_user: User, regular_user: User
) -> None:
    key = SigningPasskey.register(admin_user)
    _login(client, admin_user)
    key.enter(client)
    prior_client = app.test_client()
    _login(prior_client, regular_user)
    assert prior_client.get("/account/security").status_code == 200

    url = f"/admin/users/{regular_user.id}/active"
    assert key.post(client, url, {"active": "false"}).status_code == 302
    assert key.post(client, url, {"active": "true"}).status_code == 302
    db.session.refresh(regular_user)
    assert bool(regular_user.active) is True
    assert prior_client.get("/account/security").status_code == 302
    _login(prior_client, regular_user)
    assert prior_client.get("/account/security").status_code == 200


type InventoryRow = Organization | Project | Application


@pytest.fixture
def inventory(app: Flask) -> Iterator[dict[str, tuple[InventoryRow, InventoryRow]]]:
    marker = uuid.uuid4().hex[:12]
    rows: dict[str, list[InventoryRow]] = {
        "organizations": [],
        "projects": [],
        "applications": [],
    }
    for deleted in (False, True):
        slug = f"console-{marker}-{int(deleted)}"
        deleted_at = datetime.datetime.now(datetime.timezone.utc) if deleted else None
        org = Organization(name=slug, slug=slug, deleted_at=deleted_at)
        db.session.add(org)
        db.session.flush()
        project = Project(
            name=slug, slug=slug, organization_id=org.id, deleted_at=deleted_at
        )
        db.session.add(project)
        db.session.flush()
        environment = Environment(
            name="Production",
            slug="production",
            project_id=project.id,
            is_default=True,
            deleted_at=deleted_at,
        )
        application = Application(
            name=slug, slug=slug, project_id=project.id, deleted_at=deleted_at
        )
        db.session.add_all([environment, application])
        db.session.flush()
        db.session.add(
            ApplicationEnvironment(
                application_id=application.id,
                environment_id=environment.id,
                deleted_at=deleted_at,
            )
        )
        rows["organizations"].append(org)
        rows["projects"].append(project)
        rows["applications"].append(application)
    db.session.commit()
    try:
        yield {kind: (items[0], items[1]) for kind, items in rows.items()}
    finally:
        db.session.rollback()
        # HTTP tests commit their fixtures because the request-scoped client uses
        # a different DB session. Include any children added by those tests.
        organization_ids = [row.id for row in rows["organizations"]]
        project_ids = list(
            db.session.scalars(
                select(Project.id).where(Project.organization_id.in_(organization_ids))
            )
        )
        application_ids = list(
            db.session.scalars(
                select(Application.id).where(Application.project_id.in_(project_ids))
            )
        )
        Deployment.query.filter(Deployment.application_id.in_(application_ids)).delete(
            synchronize_session=False
        )
        Configuration.query.filter(
            Configuration.application_id.in_(application_ids)
        ).delete(synchronize_session=False)
        EnvironmentConfiguration.query.filter(
            EnvironmentConfiguration.project_id.in_(project_ids)
        ).delete(synchronize_session=False)
        ApplicationEnvironment.query.filter(
            ApplicationEnvironment.application_id.in_(application_ids)
        ).delete(synchronize_session=False)
        Application.query.filter(Application.id.in_(application_ids)).delete(
            synchronize_session=False
        )
        Environment.query.filter(Environment.project_id.in_(project_ids)).delete(
            synchronize_session=False
        )
        Project.query.filter(Project.id.in_(project_ids)).delete(
            synchronize_session=False
        )
        Organization.query.filter(
            Organization.id.in_([row.id for row in rows["organizations"]])
        ).delete(synchronize_session=False)
        db.session.commit()


@pytest.mark.parametrize("kind", ["organizations", "projects", "applications"])
def test_inventory_separates_active_and_deleted_records(
    client: FlaskClient,
    admin_user: User,
    inventory: dict[str, tuple[InventoryRow, InventoryRow]],
    kind: str,
) -> None:
    key = SigningPasskey.register(admin_user)
    _login(client, admin_user)
    key.enter(client)
    active, deleted = inventory[kind]
    active_link = f'href="/admin/{kind}/{active.id}"'.encode()
    deleted_link = f'href="/admin/{kind}/{deleted.id}"'.encode()
    for state, expected_active, expected_deleted in [
        ("active", True, False),
        ("deleted", False, True),
        ("all", True, True),
    ]:
        response = client.get(
            f"/admin/{kind}",
            query_string={
                "state": state,
                "q": active.name.rsplit("-", 1)[0],
            },
        )
        assert response.status_code == 200
        assert (active_link in response.data) is expected_active
        assert (deleted_link in response.data) is expected_deleted


def _live_app_env(
    inventory: dict[str, tuple[InventoryRow, InventoryRow]],
) -> ApplicationEnvironment:
    return db.session.execute(
        select(ApplicationEnvironment).where(
            ApplicationEnvironment.application_id == inventory["applications"][0].id
        )
    ).scalar_one()


def _deployment(
    app_env: ApplicationEnvironment,
    *,
    created: datetime.datetime,
    complete: bool = False,
    error: bool = False,
    deployment_id: uuid.UUID | None = None,
) -> Deployment:
    deployment = Deployment(
        id=deployment_id or uuid.uuid4(),
        application_id=app_env.application_id,
        application_environment_id=app_env.id,
        release={},
        complete=complete,
        error=error,
        created=created,
    )
    db.session.add(deployment)
    db.session.flush()
    return deployment


def test_triage_latest_before_status_and_deterministic_ties(
    inventory: dict[str, tuple[InventoryRow, InventoryRow]],
) -> None:
    app_env = _live_app_env(inventory)
    now = datetime.datetime.now(datetime.timezone.utc)
    failed = _deployment(
        app_env, created=now, error=True, deployment_id=uuid.UUID(int=1)
    )
    recovered = _deployment(
        app_env, created=now, complete=True, deployment_id=uuid.UUID(int=2)
    )
    rows, _ = queries.deployments_page(
        "failed", 1, application_id=app_env.application_id
    )
    assert rows == []
    rows, _ = queries.deployments_page(
        "attention", 1, application_id=app_env.application_id
    )
    assert rows == []
    rows, _ = queries.deployments_page("all", 1, application_id=app_env.application_id)
    assert [row.id for row in rows] == [recovered.id]
    latest = queries.latest_deployment_by_app_env([app_env.id])
    assert latest[app_env.id].id == recovered.id
    historical, _ = queries.deployments_page(
        "failed", 1, mode="history", application_id=app_env.application_id
    )
    assert [row.id for row in historical] == [failed.id]
    assert not any(row.id == failed.id for row in queries.overview().failed)


def test_deployment_filters_precede_page_limit(
    inventory: dict[str, tuple[InventoryRow, InventoryRow]],
) -> None:
    app_env = _live_app_env(inventory)
    now = datetime.datetime.now(datetime.timezone.utc)
    desired = _deployment(
        app_env, created=now - datetime.timedelta(hours=2), error=True
    )
    # Another environment in the same tenant has more than a page of newer history.
    environment = Environment(
        name="Staging",
        slug="staging",
        project_id=inventory["projects"][0].id,
    )
    db.session.add(environment)
    db.session.flush()
    other_env = ApplicationEnvironment(
        application_id=app_env.application_id, environment_id=environment.id
    )
    db.session.add(other_env)
    db.session.flush()
    for index in range(queries.PAGE_SIZE + 2):
        _deployment(
            other_env,
            created=now - datetime.timedelta(seconds=index),
            complete=True,
        )
    rows, has_next = queries.deployments_page(
        "all",
        1,
        mode="history",
        application_id=app_env.application_id,
        environment="production",
    )
    assert [row.id for row in rows] == [desired.id]
    assert not has_next
    rows, has_next = queries.deployments_page(
        "all", 1, mode="history", term=str(desired.id)
    )
    assert [row.id for row in rows] == [desired.id]
    assert not has_next
    rows, has_next = queries.deployments_page(
        "all", 1, mode="history", application_id=app_env.application_id
    )
    assert len(rows) == queries.PAGE_SIZE
    assert has_next
    rows, has_next = queries.deployments_page(
        "all", 2, mode="history", application_id=app_env.application_id
    )
    assert len(rows) == 3
    assert rows[-1].id == desired.id
    assert not has_next


def test_triage_excludes_deleted_ancestry_and_old_attempts(
    inventory: dict[str, tuple[InventoryRow, InventoryRow]],
) -> None:
    app_env = _live_app_env(inventory)
    now = datetime.datetime.now(datetime.timezone.utc)
    failed = _deployment(app_env, created=now - datetime.timedelta(days=10), error=True)
    rows, _ = queries.deployments_page(
        "attention", 1, application_id=app_env.application_id
    )
    assert rows == []
    rows, _ = queries.deployments_page(
        "attention", 1, days="30", application_id=app_env.application_id
    )
    assert [row.id for row in rows] == [failed.id]
    app_env.environment.deleted_at = now
    db.session.flush()
    rows, _ = queries.deployments_page(
        "attention", 1, days="30", application_id=app_env.application_id
    )
    assert rows == []
    rows, _ = queries.deployments_page(
        "failed", 1, days="all", mode="history", application_id=app_env.application_id
    )
    assert [row.id for row in rows] == [failed.id]


def test_scoped_activity_is_bounded_redacted_and_escaped(
    client: FlaskClient,
    admin_user: User,
    inventory: dict[str, tuple[InventoryRow, InventoryRow]],
) -> None:
    key = SigningPasskey.register(admin_user)
    _login(client, admin_user)
    key.enter(client)
    org = cast(Organization, inventory["organizations"][0])
    project = cast(Project, inventory["projects"][0])
    application = cast(Application, inventory["applications"][0])
    app_env = _live_app_env(inventory)
    application.name = '<script>alert("app")</script>'
    config = Configuration(
        application_id=application.id,
        application_environment_id=app_env.id,
        name="SECRET_TOKEN",
        value="never-display-config-value",
        secret=True,
    )
    db.session.add(config)
    sibling_app = Application(name="Sibling app", slug="sibling", project_id=project.id)
    sibling_project = Project(
        name="Sibling project", slug="sibling", organization_id=org.id
    )
    db.session.add_all([sibling_app, sibling_project])
    db.session.flush()
    deployment = _deployment(
        app_env, created=datetime.datetime.now(datetime.timezone.utc), error=True
    )
    deployment.error_detail = "never-display-deployment-error-payload"
    db.session.flush()
    Activity = activity_plugin.activity_cls
    events = [
        Activity(
            verb="create",
            object=config,
            data={"user_id": str(admin_user.id), "value": "never-display-raw-data"},
        ),
        Activity(
            verb="edit",
            object=org,
            data={
                "user_id": str(admin_user.id),
                "action": "add_member",
                "member_email": "<member>@example.com",
                "value": "never-display-member-payload",
            },
        ),
        Activity(
            verb="edit",
            object=project,
            data={
                "user_id": str(admin_user.id),
                "action": "never-display-action",
                "member_email": "never-display-unrelated-member-field",
            },
        ),
        Activity(
            verb="edit",
            object=app_env.environment,
            data={"user_id": str(admin_user.id)},
        ),
        Activity(
            verb="edit",
            object=inventory["organizations"][1],
            data={"user_id": str(admin_user.id), "action": "remove_member"},
        ),
        Activity(verb="edit", object=sibling_app, data={"user_id": str(admin_user.id)}),
        Activity(
            verb="edit", object=sibling_project, data={"user_id": str(admin_user.id)}
        ),
        Activity(
            verb="create", object=deployment, data={"user_id": str(admin_user.id)}
        ),
    ]
    db.session.add_all(events)
    db.session.flush()
    organization_rows = queries.resource_activity(organization_id=org.id)
    project_rows = queries.resource_activity(
        organization_id=org.id, project_id=project.id
    )
    app_rows = queries.resource_activity(
        organization_id=org.id, project_id=project.id, application_id=application.id
    )
    assert {row.id for row in organization_rows} == {
        event.id for index, event in enumerate(events) if index != 4
    }
    assert {row.id for row in project_rows} == {events[i].id for i in (0, 2, 3, 5, 7)}
    assert {row.id for row in app_rows} == {events[0].id, events[7].id}
    assert "Configuration SECRET_TOKEN created" in {row.summary for row in app_rows}
    assert all(row.actor == admin_user.username for row in organization_rows)
    assert (
        queries.resource_activity(
            organization_id=inventory["organizations"][1].id,
            project_id=project.id,
        )
        == []
    )
    db.session.commit()
    response = client.get(f"/admin/applications/{application.id}")
    assert response.status_code == 200
    assert b"&lt;script&gt;" in response.data
    assert b'<script>alert("app")</script>' not in response.data
    assert b"never-display-config-value" not in response.data
    assert b"never-display-raw-data" not in response.data
    assert b"never-display-deployment-error-payload" not in response.data
    response = client.get(f"/admin/projects/{project.id}")
    assert b"never-display-action" not in response.data
    assert b"never-display-unrelated-member-field" not in response.data
    response = client.get(f"/admin/organizations/{org.id}")
    assert b"Member added: &lt;member&gt;@example.com" in response.data
    assert b"never-display-member-payload" not in response.data
    assert b"never-display-unrelated-member-field" not in response.data
    for _ in range(queries.ACTIVITY_LIMIT + 2):
        db.session.add(Activity(verb="edit", object=project, data={}))
    db.session.flush()
    rows = queries.resource_activity(organization_id=org.id, project_id=project.id)
    assert len(rows) == queries.ACTIVITY_LIMIT
    assert [row.id for row in rows] == sorted((row.id for row in rows), reverse=True)


def test_attention_links_active_admins_missing_passkeys(
    client: FlaskClient, admin_user: User, regular_user: User
) -> None:
    key = SigningPasskey.register(admin_user)
    _login(client, admin_user)
    key.enter(client)
    regular_user.admin = True
    regular_user.active = True
    db.session.commit()
    overview = queries.overview()
    assert regular_user.id in {user.id for user in overview.admins_without_passkeys}
    assert admin_user.id not in {user.id for user in overview.admins_without_passkeys}
    response = client.get("/admin/")
    assert response.status_code == 200
    assert f'href="/admin/users/{regular_user.id}"'.encode() in response.data
    regular_user.active = False
    db.session.commit()
    assert regular_user.id not in {
        user.id for user in queries.overview().admins_without_passkeys
    }
    response = client.get("/admin/")
    assert response.status_code == 200
    assert f'href="/admin/users/{regular_user.id}"'.encode() not in response.data
    db.session.rollback()


def test_deployment_route_filters_and_invalid_scope(
    client: FlaskClient,
    admin_user: User,
    inventory: dict[str, tuple[InventoryRow, InventoryRow]],
) -> None:
    key = SigningPasskey.register(admin_user)
    _login(client, admin_user)
    key.enter(client)
    app_env = _live_app_env(inventory)
    failed = _deployment(
        app_env, created=datetime.datetime.now(datetime.timezone.utc), error=True
    )
    db.session.commit()
    response = client.get(
        "/admin/deployments",
        query_string={
            "application_id": str(app_env.application_id),
            "env": "production",
            "q": str(failed.id),
        },
    )
    assert response.status_code == 200
    application = cast(Application, inventory["applications"][0])
    with client.application.test_request_context():
        deployment_url = url_for(
            "user.deployment_detail",
            org_slug=application.project.organization.slug,
            project_slug=application.project.slug,
            app_slug=application.slug,
            deployment_id=failed.id,
        )
    assert f'href="{deployment_url}"'.encode() in response.data
    assert b"mode=history" in response.data
    assert (
        client.get(
            "/admin/deployments", query_string={"project_id": "not-a-uuid"}
        ).status_code
        == 400
    )


@pytest.mark.parametrize(
    ("action", "summary"),
    [
        ("add_member", "Member added"),
        ("remove_member", "Member removed"),
        ("promote_member", "Member promoted to admin"),
        ("demote_member", "Member admin access removed"),
    ],
)
def test_activity_identifies_affected_member(
    inventory: dict[str, tuple[InventoryRow, InventoryRow]],
    action: str,
    summary: str,
) -> None:
    org = inventory["organizations"][0]
    Activity = activity_plugin.activity_cls
    db.session.add(
        Activity(
            verb="edit",
            object=org,
            data={"action": action, "member_email": "member@example.com"},
        )
    )
    db.session.flush()
    rows = queries.resource_activity(organization_id=org.id)
    assert [row.summary for row in rows] == [f"{summary}: member@example.com"]


@pytest.mark.parametrize("verb", ["create", "edit", "delete"])
def test_shared_configuration_activity_scope_and_redaction(
    client: FlaskClient,
    admin_user: User,
    inventory: dict[str, tuple[InventoryRow, InventoryRow]],
    verb: str,
) -> None:
    key = SigningPasskey.register(admin_user)
    _login(client, admin_user)
    key.enter(client)
    org = cast(Organization, inventory["organizations"][0])
    project = cast(Project, inventory["projects"][0])
    app_env = _live_app_env(inventory)
    environment = app_env.environment
    other_environment = Environment(
        name="Other shared scope", slug="other-shared", project_id=project.id
    )
    config = EnvironmentConfiguration(
        project_id=project.id,
        environment_id=environment.id,
        name="SHARED_PRIVATE_TOKEN",
        value="never-display-shared-value",
        secret=True,
        buildtime=True,
    )
    db.session.add_all([config, other_environment])
    db.session.commit()
    config_id = config.id
    try:
        if verb == "edit":
            config.value = "never-display-edited-shared-value"
        elif verb == "delete":
            db.session.delete(config)
        db.session.flush()
        Activity = activity_plugin.activity_cls
        event = Activity(verb=verb, object=config, data={"user_id": str(admin_user.id)})
        db.session.add(event)
        db.session.flush()
        entry = db.session.get(AuditLog, event.id)
        assert entry is not None
        assert entry.object_type == "EnvironmentConfiguration"
        assert entry.object_name == "SHARED_PRIVATE_TOKEN"
        assert entry.project_id == project.id
        assert entry.organization_id == org.id
        assert entry.application_id is None
        assert entry.application_environment_id is None
        assert entry.config_secret is True
        assert entry.config_buildtime is True
        assert "never-display" not in str(entry.raw_data)
        organization_rows = queries.resource_activity(organization_id=org.id)
        project_rows = queries.resource_activity(
            organization_id=org.id, project_id=project.id
        )
        assert event.id in {row.id for row in organization_rows}
        assert event.id in {row.id for row in project_rows}
        assert any(
            row.summary.startswith("Shared configuration SHARED_PRIVATE_TOKEN ")
            for row in project_rows
        )
        assert (
            queries.resource_activity(
                organization_id=org.id,
                project_id=project.id,
                application_id=app_env.application_id,
            )
            == []
        )
        assert (
            queries.resource_activity(organization_id=inventory["organizations"][1].id)
            == []
        )
        db.session.commit()
        response = client.get(f"/admin/projects/{project.id}")
        assert response.status_code == 200
        assert b"Shared configuration SHARED_PRIVATE_TOKEN" in response.data
        assert b"never-display" not in response.data
        native_base = f"/projects/{org.slug}/{project.slug}/env"
        response = client.get(f"{native_base}/{environment.slug}/audit")
        assert response.status_code == 200
        assert b"Shared configuration" in response.data
        assert b"SHARED_PRIVATE_TOKEN" in response.data
        assert b"never-display" not in response.data
        response = client.get(f"{native_base}/{other_environment.slug}/audit")
        assert response.status_code == 200
        assert b"SHARED_PRIVATE_TOKEN" not in response.data
    finally:
        db.session.rollback()
        EnvironmentConfiguration.query.filter(
            EnvironmentConfiguration.id == config_id
        ).delete(synchronize_session=False)
        db.session.commit()
