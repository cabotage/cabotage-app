"""Tests for the audit_log SQL view."""

import uuid

import pytest

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Configuration,
    Environment,
    Image,
    Project,
    activity_plugin,
)
from cabotage.server.wsgi import app as _app

Activity = activity_plugin.activity_cls

REPOSITORY_NAME = "cabotage/testorg/testproj/webapp"


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    with _app.app_context():
        yield _app


@pytest.fixture
def db_session(app):
    yield db.session
    db.session.rollback()


@pytest.fixture
def org(db_session):
    o = Organization(name="AuditOrg", slug=f"auditorg-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    db_session.flush()
    return o


@pytest.fixture
def project(db_session, org):
    p = Project(name="AuditProject", organization_id=org.id)
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture
def environment(db_session, project):
    e = Environment(name="default", project_id=project.id, ephemeral=False)
    db_session.add(e)
    db_session.flush()
    return e


@pytest.fixture
def application(db_session, project):
    a = Application(name="auditapp", slug="auditapp", project_id=project.id)
    db_session.add(a)
    db_session.flush()
    return a


@pytest.fixture
def app_env(db_session, application, environment):
    ae = ApplicationEnvironment(
        application_id=application.id, environment_id=environment.id
    )
    db_session.add(ae)
    db_session.flush()
    return ae


class TestAuditViewExists:
    def test_view_is_queryable(self, db_session):
        rows = db_session.execute(db.text("SELECT * FROM audit_log LIMIT 1")).fetchall()
        assert rows is not None

    def test_view_has_expected_columns(self, db_session):
        row = db_session.execute(db.text("SELECT * FROM audit_log LIMIT 0"))
        columns = [c[0] for c in row.cursor.description]
        expected = [
            "id",
            "timestamp",
            "object_tx_id",
            "transaction_id",
            "verb",
            "detail",
            "object_type",
            "object_id",
            "object_name",
            "application_id",
            "application_environment_id",
            "project_id",
            "organization_id",
            "app_name",
            "project_name",
            "actor_username",
            "actor_email",
            "remote_addr",
            "config_secret",
            "config_buildtime",
            "config_version",
            "image_ref",
            "image_sha",
            "deploy_release_version",
            "raw_data",
        ]
        assert columns == expected


class TestAuditViewConfigActivity:
    def test_config_create_appears(self, db_session, application, app_env):
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="AUDIT_TEST_VAR",
            value="hello",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()
        activity = Activity(
            verb="create",
            object=cfg,
            data={"user_id": "test-user", "timestamp": "2026-01-01T00:00:00"},
        )
        db_session.add(activity)
        db_session.flush()

        rows = db_session.execute(
            db.text(
                "SELECT verb, object_type, object_name, application_id "
                "FROM audit_log WHERE object_name = 'AUDIT_TEST_VAR'"
            )
        ).fetchall()
        assert len(rows) == 1
        assert rows[0].verb == "create"
        assert rows[0].object_type == "Configuration"
        assert rows[0].application_id == application.id

    def test_config_secret_flag(self, db_session, application, app_env):
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="SECRET_VAR",
            value="shh",
            secret=True,
        )
        db_session.add(cfg)
        db_session.flush()
        activity = Activity(
            verb="create",
            object=cfg,
            data={"user_id": "test-user", "timestamp": "2026-01-01T00:00:00"},
        )
        db_session.add(activity)
        db_session.flush()

        rows = db_session.execute(
            db.text(
                "SELECT config_secret, config_buildtime "
                "FROM audit_log WHERE object_name = 'SECRET_VAR'"
            )
        ).fetchall()
        assert rows[0].config_secret is True
        assert rows[0].config_buildtime is False


class TestAuditViewImageActivity:
    def test_image_shows_ref(self, db_session, application, app_env):
        img = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image_metadata={"sha": "abc123", "trigger": "manual_build"},
            build_ref="develop",
        )
        db_session.add(img)
        db_session.flush()
        activity = Activity(
            verb="fromsource",
            object=img,
            data={"user_id": "test-user", "timestamp": "2026-01-01T00:00:00"},
        )
        db_session.add(activity)
        db_session.flush()

        rows = db_session.execute(
            db.text(
                "SELECT object_name, image_ref, image_sha "
                "FROM audit_log WHERE object_type = 'Image' "
                "AND application_id = :app_id"
            ),
            {"app_id": application.id},
        ).fetchall()
        assert len(rows) >= 1
        assert rows[0].image_ref == "develop"
        assert rows[0].image_sha == "abc123"
        assert rows[0].object_name.startswith("#")


class TestAuditViewFiltering:
    def test_complete_error_excluded(self, db_session, application, app_env):
        img = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image_metadata={},
            build_ref="main",
        )
        db_session.add(img)
        db_session.flush()
        # Create a "complete" activity — should be filtered out
        activity = Activity(
            verb="complete",
            object=img,
            data={"user_id": "automation", "timestamp": "2026-01-01T00:00:00"},
        )
        db_session.add(activity)
        db_session.flush()

        rows = db_session.execute(
            db.text(
                "SELECT * FROM audit_log "
                "WHERE verb = 'complete' AND object_type = 'Image' "
                "AND application_id = :app_id"
            ),
            {"app_id": application.id},
        ).fetchall()
        assert len(rows) == 0

    def test_scoping_by_application_id(self, db_session, application, app_env, project):
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="SCOPED_VAR",
            value="yes",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()
        activity = Activity(
            verb="create",
            object=cfg,
            data={"user_id": "test", "timestamp": "2026-01-01T00:00:00"},
        )
        db_session.add(activity)
        db_session.flush()

        # Should appear for this app
        rows = db_session.execute(
            db.text(
                "SELECT * FROM audit_log "
                "WHERE application_id = :app_id AND object_name = 'SCOPED_VAR'"
            ),
            {"app_id": application.id},
        ).fetchall()
        assert len(rows) == 1

        # Should NOT appear for a random UUID
        rows = db_session.execute(
            db.text(
                "SELECT * FROM audit_log "
                "WHERE application_id = :app_id AND object_name = 'SCOPED_VAR'"
            ),
            {"app_id": uuid.uuid4()},
        ).fetchall()
        assert len(rows) == 0
