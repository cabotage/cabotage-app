"""Tests for notification routing API."""

import time
import uuid

import pytest
from flask_security import hash_password

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.notifications import NotificationRoute
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    Environment,
    Project,
    activity_plugin,
)
from cabotage.server.wsgi import app as _app

Activity = activity_plugin.activity_cls


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REQUIRE_MFA"] = False
    with _app.app_context():
        yield _app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_user(app):
    u = User(
        username=f"testadmin-{uuid.uuid4().hex[:8]}",
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.execute(
        db.text("DELETE FROM activity WHERE object_id = :uid"), {"uid": u.id}
    )
    db.session.execute(
        db.text("UPDATE transaction SET user_id = NULL WHERE user_id = :uid"),
        {"uid": u.id},
    )
    db.session.delete(u)
    db.session.commit()


@pytest.fixture
def non_admin_user(app):
    u = User(
        username=f"testuser-{uuid.uuid4().hex[:8]}",
        email=f"user-{uuid.uuid4().hex[:8]}@example.com",
        password=hash_password("password123"),
        active=True,
        fs_uniquifier=uuid.uuid4().hex,
    )
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.execute(
        db.text("DELETE FROM activity WHERE object_id = :uid"), {"uid": u.id}
    )
    db.session.execute(
        db.text("UPDATE transaction SET user_id = NULL WHERE user_id = :uid"),
        {"uid": u.id},
    )
    db.session.delete(u)
    db.session.commit()


@pytest.fixture
def org(admin_user):
    o = Organization(name="Test Org", slug=f"testorg-{uuid.uuid4().hex[:8]}")
    db.session.add(o)
    db.session.flush()
    membership = OrganizationMember(
        organization_id=o.id, user_id=admin_user.id, admin=True
    )
    db.session.add(membership)
    db.session.commit()
    yield o
    db.session.rollback()
    NotificationRoute.query.filter_by(organization_id=o.id).delete()
    OrganizationMember.query.filter_by(organization_id=o.id).delete()
    db.session.flush()
    db.session.delete(o)
    db.session.commit()


@pytest.fixture
def project(org):
    p = Project(name="My Project", organization_id=org.id)
    db.session.add(p)
    db.session.flush()
    return p


@pytest.fixture
def environment(project):
    e = Environment(name="production", project_id=project.id, ephemeral=False)
    db.session.add(e)
    db.session.flush()
    return e


@pytest.fixture
def application(project):
    a = Application(name="webapp", slug="webapp", project_id=project.id)
    db.session.add(a)
    db.session.flush()
    return a


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"


class TestListRoutes:
    def test_list_empty(self, client, admin_user, org):
        _login(client, admin_user)
        resp = client.get(
            f"/integrations/notifications/{org.slug}/routes?category=pipeline"
        )
        assert resp.status_code == 200
        assert resp.get_json()["routes"] == []

    def test_list_with_routes(self, client, admin_user, org):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["pipeline.deploy"],
            integration="slack",
            channel_id="C001",
            channel_name="deploys",
            enabled=True,
        )
        db.session.add(route)
        db.session.commit()

        _login(client, admin_user)
        resp = client.get(
            f"/integrations/notifications/{org.slug}/routes?category=pipeline"
        )
        assert resp.status_code == 200
        routes = resp.get_json()["routes"]
        assert len(routes) == 1
        assert routes[0]["channel_name"] == "deploys"
        assert routes[0]["integration"] == "slack"

    def test_list_all_no_category(self, client, admin_user, org):
        r1 = NotificationRoute(
            organization_id=org.id,
            notification_types=["pipeline.deploy"],
            integration="slack",
            channel_id="C001",
            enabled=True,
        )
        r2 = NotificationRoute(
            organization_id=org.id,
            notification_types=["health.oom"],
            integration="discord",
            channel_id="D001",
            enabled=True,
        )
        db.session.add_all([r1, r2])
        db.session.commit()

        _login(client, admin_user)
        resp = client.get(f"/integrations/notifications/{org.slug}/routes")
        routes = resp.get_json()["routes"]
        assert len(routes) == 2

    def test_list_multi_type_rule(self, client, admin_user, org):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["pipeline.deploy", "pipeline.release", "health.oom"],
            integration="slack",
            channel_id="C001",
            enabled=True,
        )
        db.session.add(route)
        db.session.commit()

        _login(client, admin_user)
        resp = client.get(
            f"/integrations/notifications/{org.slug}/routes?category=pipeline"
        )
        routes = resp.get_json()["routes"]
        assert len(routes) == 1
        assert set(routes[0]["notification_types"]) == {
            "pipeline.deploy",
            "pipeline.release",
            "health.oom",
        }

    def test_invalid_category(self, client, admin_user, org):
        _login(client, admin_user)
        resp = client.get(
            f"/integrations/notifications/{org.slug}/routes?category=invalid"
        )
        assert resp.status_code == 400

    def test_requires_admin(self, client, non_admin_user, org):
        _login(client, non_admin_user)
        resp = client.get(
            f"/integrations/notifications/{org.slug}/routes?category=pipeline"
        )
        assert resp.status_code == 403


class TestSaveRoute:
    def test_create_route(self, client, admin_user, org):
        _login(client, admin_user)
        resp = client.post(
            f"/integrations/notifications/{org.slug}/routes",
            json={
                "notification_types": ["pipeline.deploy"],
                "integration": "slack",
                "channel_id": "C001",
                "channel_name": "deploys",
                "enabled": True,
            },
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["notification_types"] == ["pipeline.deploy"]
        assert data["integration"] == "slack"
        assert data["channel_id"] == "C001"
        assert data["project_ids"] == []

        # Verify Activity was recorded
        activity = Activity.query.filter(
            Activity.object_id == org.id,
            Activity.verb == "create",
            Activity.data["action"].astext == "notification_route_create",
        ).first()
        assert activity is not None

    def test_create_with_multi_scope(
        self, client, admin_user, org, project, application
    ):
        _login(client, admin_user)
        resp = client.post(
            f"/integrations/notifications/{org.slug}/routes",
            json={
                "notification_types": ["health.oom"],
                "project_ids": [str(project.id)],
                "application_ids": [str(application.id)],
                "integration": "discord",
                "channel_id": "D001",
                "channel_name": "health-alerts",
                "enabled": True,
            },
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["project_ids"] == [str(project.id)]
        assert data["application_ids"] == [str(application.id)]

    def test_update_existing(self, client, admin_user, org):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["http.5xx"],
            integration="slack",
            channel_id="C001",
            channel_name="old",
            enabled=True,
        )
        db.session.add(route)
        db.session.commit()

        _login(client, admin_user)
        resp = client.post(
            f"/integrations/notifications/{org.slug}/routes",
            json={
                "id": str(route.id),
                "notification_types": ["http.5xx"],
                "integration": "slack",
                "channel_id": "C999",
                "channel_name": "new-channel",
                "enabled": False,
            },
        )
        assert resp.status_code == 201
        db.session.refresh(route)
        assert route.channel_id == "C999"
        assert route.enabled is False

        # Verify Activity was recorded
        activity = Activity.query.filter(
            Activity.object_id == org.id,
            Activity.verb == "edit",
            Activity.data["action"].astext == "notification_route_edit",
        ).first()
        assert activity is not None

    def test_invalid_type(self, client, admin_user, org):
        _login(client, admin_user)
        resp = client.post(
            f"/integrations/notifications/{org.slug}/routes",
            json={
                "notification_types": ["invalid.type"],
                "integration": "slack",
                "channel_id": "C001",
            },
        )
        assert resp.status_code == 400

    def test_invalid_integration(self, client, admin_user, org):
        _login(client, admin_user)
        resp = client.post(
            f"/integrations/notifications/{org.slug}/routes",
            json={
                "notification_types": ["pipeline.deploy"],
                "integration": "teams",
                "channel_id": "C001",
            },
        )
        assert resp.status_code == 400

    def test_requires_channel(self, client, admin_user, org):
        _login(client, admin_user)
        resp = client.post(
            f"/integrations/notifications/{org.slug}/routes",
            json={
                "notification_types": ["pipeline.deploy"],
                "integration": "slack",
            },
        )
        assert resp.status_code == 400

    def test_requires_admin(self, client, non_admin_user, org):
        _login(client, non_admin_user)
        resp = client.post(
            f"/integrations/notifications/{org.slug}/routes",
            json={
                "notification_types": ["pipeline.deploy"],
                "integration": "slack",
                "channel_id": "C001",
            },
        )
        assert resp.status_code == 403


class TestDeleteRoute:
    def test_delete(self, client, admin_user, org):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["pipeline.deploy"],
            integration="slack",
            channel_id="C001",
            enabled=True,
        )
        db.session.add(route)
        db.session.commit()
        route_id = route.id

        _login(client, admin_user)
        resp = client.delete(
            f"/integrations/notifications/{org.slug}/routes/{route_id}"
        )
        assert resp.status_code == 200
        assert db.session.get(NotificationRoute, route_id) is None

        # Verify Activity was recorded
        activity = Activity.query.filter(
            Activity.object_id == org.id,
            Activity.verb == "delete",
            Activity.data["action"].astext == "notification_route_delete",
        ).first()
        assert activity is not None

    def test_requires_admin(self, client, non_admin_user, org):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["pipeline.deploy"],
            integration="slack",
            channel_id="C001",
            enabled=True,
        )
        db.session.add(route)
        db.session.commit()

        _login(client, non_admin_user)
        resp = client.delete(
            f"/integrations/notifications/{org.slug}/routes/{route.id}"
        )
        assert resp.status_code == 403


class TestListScopes:
    def test_list_scopes(
        self, client, admin_user, org, project, environment, application
    ):
        _login(client, admin_user)
        resp = client.get(f"/integrations/notifications/{org.slug}/scopes")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["projects"]) == 1
        assert data["projects"][0]["slug"] == project.slug
        assert len(data["projects"][0]["environments"]) == 1
        assert len(data["projects"][0]["applications"]) == 1

    def test_requires_admin(self, client, non_admin_user, org):
        _login(client, non_admin_user)
        resp = client.get(f"/integrations/notifications/{org.slug}/scopes")
        assert resp.status_code == 403
