import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Alert,
    Application,
    ApplicationEnvironment,
    Environment,
    Project,
)
from cabotage.server.wsgi import app as _app


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["ALERTMANAGER_WEBHOOK_SECRET"] = "test-secret"
    _app.config["ALERTMANAGER_URL"] = "http://alertmanager:9093"
    _app.config["ALERTMANAGER_VERIFY"] = False
    with _app.app_context():
        yield _app


@pytest.fixture
def db_session(app):
    yield db.session
    db.session.rollback()


@pytest.fixture
def org(db_session):
    o = Organization(name="Test Org", slug=f"testorg-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    db_session.flush()
    return o


@pytest.fixture
def project(db_session, org):
    p = Project(name="My Project", organization_id=org.id)
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
    a = Application(name="webapp", slug="webapp", project_id=project.id)
    db_session.add(a)
    db_session.flush()
    return a


@pytest.fixture
def app_env(db_session, application, environment):
    ae = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
    )
    db_session.add(ae)
    db_session.flush()
    return ae


def _am_v2_alert(
    fingerprint,
    alertname="ResidentDeploymentOOMKilled",
    state="active",
    starts_at="2026-03-30T17:57:58Z",
    labels=None,
):
    """Build an Alertmanager v2 API alert object."""
    if labels is None:
        labels = {
            "alertname": alertname,
            "severity": "critical",
        }
    return {
        "fingerprint": fingerprint,
        "status": {"state": state},
        "labels": labels,
        "annotations": {"summary": f"Alert {alertname}"},
        "startsAt": starts_at,
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "/graph?g0.expr=test",
    }


def _mock_am_response(alerts, status_code=200):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = alerts
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


STARTS_AT = datetime(2026, 3, 30, 17, 57, 58)
STARTS_AT_STR = "2026-03-30T17:57:58Z"


def _run_reconcile(db_session):
    from cabotage.celery.tasks.alerting import reconcile_alerts

    reconcile_alerts()
    db_session.expire_all()


class TestReconcileAlerts:
    @patch("cabotage.celery.tasks.alerting.requests.get")
    def test_noop_when_url_not_configured(self, mock_get, app, db_session):
        _app.config["ALERTMANAGER_URL"] = None
        try:
            _run_reconcile(db_session)
            mock_get.assert_not_called()
        finally:
            _app.config["ALERTMANAGER_URL"] = "http://alertmanager:9093"

    @patch("cabotage.celery.tasks.alerting.requests.get")
    def test_inserts_new_alert_from_api(self, mock_get, app, db_session):
        fingerprint = uuid.uuid4().hex[:16]
        mock_get.return_value = _mock_am_response([_am_v2_alert(fingerprint)])

        _run_reconcile(db_session)

        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert is not None
        assert alert.status == "firing"
        assert alert.alertname == "ResidentDeploymentOOMKilled"

    @patch("cabotage.celery.tasks.alerting.requests.get")
    def test_updates_existing_alert(self, mock_get, app, db_session):
        fingerprint = uuid.uuid4().hex[:16]

        existing = Alert(
            fingerprint=fingerprint,
            status="firing",
            alertname="ResidentDeploymentOOMKilled",
            labels={"alertname": "ResidentDeploymentOOMKilled"},
            annotations={},
            starts_at=STARTS_AT,
        )
        db_session.add(existing)
        db_session.commit()
        original_id = existing.id

        mock_get.return_value = _mock_am_response(
            [
                _am_v2_alert(
                    fingerprint,
                    labels={
                        "alertname": "ResidentDeploymentOOMKilled",
                        "severity": "critical",
                        "extra": "label",
                    },
                )
            ]
        )

        _run_reconcile(db_session)

        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.id == original_id
        assert alert.labels["extra"] == "label"

    @patch("cabotage.celery.tasks.alerting.requests.get")
    def test_resolves_missing_alerts(self, mock_get, app, db_session):
        fingerprint = uuid.uuid4().hex[:16]

        existing = Alert(
            fingerprint=fingerprint,
            status="firing",
            alertname="ResidentDeploymentOOMKilled",
            labels={"alertname": "ResidentDeploymentOOMKilled"},
            annotations={},
            starts_at=STARTS_AT,
        )
        db_session.add(existing)
        db_session.commit()

        mock_get.return_value = _mock_am_response([])

        _run_reconcile(db_session)

        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.status == "resolved"
        assert alert.ends_at is not None

    @patch("cabotage.celery.tasks.alerting.requests.get")
    def test_does_not_resolve_already_resolved(self, mock_get, app, db_session):
        fingerprint = uuid.uuid4().hex[:16]
        ends_at = datetime(2026, 3, 30, 18, 5, 0)

        existing = Alert(
            fingerprint=fingerprint,
            status="resolved",
            alertname="ResidentDeploymentOOMKilled",
            labels={"alertname": "ResidentDeploymentOOMKilled"},
            annotations={},
            starts_at=STARTS_AT,
            ends_at=ends_at,
        )
        db_session.add(existing)
        db_session.commit()

        mock_get.return_value = _mock_am_response([])

        _run_reconcile(db_session)

        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.status == "resolved"
        assert alert.ends_at == ends_at  # unchanged

    @patch("cabotage.celery.tasks.alerting.requests.get")
    def test_resolves_application_on_reconcile(
        self, mock_get, app, db_session, org, project, application, app_env
    ):
        fingerprint = uuid.uuid4().hex[:16]
        deployment_name = f"{project.k8s_identifier}-{application.k8s_identifier}"
        # Commit so the task's queries can see the fixtures
        db_session.commit()

        mock_get.return_value = _mock_am_response(
            [
                _am_v2_alert(
                    fingerprint,
                    labels={
                        "alertname": "ResidentDeploymentOOMKilled",
                        "deployment": deployment_name,
                        "namespace": org.k8s_identifier,
                        "severity": "critical",
                    },
                )
            ]
        )

        _run_reconcile(db_session)

        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.application_id == application.id
        assert alert.application_environment_id == app_env.id

    @patch("cabotage.celery.tasks.alerting.requests.get")
    def test_backfills_application_on_existing_alert(
        self, mock_get, app, db_session, org, project, application, app_env
    ):
        fingerprint = uuid.uuid4().hex[:16]
        deployment_name = f"{project.k8s_identifier}-{application.k8s_identifier}"

        existing = Alert(
            fingerprint=fingerprint,
            status="firing",
            alertname="ResidentDeploymentOOMKilled",
            labels={"alertname": "ResidentDeploymentOOMKilled"},
            annotations={},
            starts_at=STARTS_AT,
        )
        db_session.add(existing)
        # Commit both the alert and the fixtures
        db_session.commit()
        assert existing.application_id is None

        mock_get.return_value = _mock_am_response(
            [
                _am_v2_alert(
                    fingerprint,
                    labels={
                        "alertname": "ResidentDeploymentOOMKilled",
                        "deployment": deployment_name,
                        "namespace": org.k8s_identifier,
                        "severity": "critical",
                    },
                )
            ]
        )

        _run_reconcile(db_session)

        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.application_id == application.id

    @patch("cabotage.celery.tasks.alerting.requests.get")
    def test_handles_api_error_gracefully(self, mock_get, app, db_session):
        import requests as req

        mock_get.side_effect = req.ConnectionError("refused")

        # Should not raise
        _run_reconcile(db_session)

    @patch("cabotage.celery.tasks.alerting.requests.get")
    def test_sends_bearer_token(self, mock_get, app, db_session):
        mock_get.return_value = _mock_am_response([])

        _run_reconcile(db_session)

        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer test-secret"

    @patch("cabotage.celery.tasks.alerting.requests.get")
    def test_maps_active_state_to_firing(self, mock_get, app, db_session):
        """The v2 API uses 'active' state, we store as 'firing'."""
        fingerprint = uuid.uuid4().hex[:16]
        mock_get.return_value = _mock_am_response(
            [_am_v2_alert(fingerprint, state="active")]
        )

        _run_reconcile(db_session)

        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.status == "firing"

    @patch("cabotage.celery.tasks.alerting.requests.get")
    def test_preserves_suppressed_state(self, mock_get, app, db_session):
        fingerprint = uuid.uuid4().hex[:16]
        mock_get.return_value = _mock_am_response(
            [_am_v2_alert(fingerprint, state="suppressed")]
        )

        _run_reconcile(db_session)

        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.status == "suppressed"
