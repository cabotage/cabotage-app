import datetime
import uuid
from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select

from cabotage.server import db
from cabotage.server.admin_console import queries
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Project,
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
        # HTTP tests commit fixtures because the client uses a different DB session.
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
