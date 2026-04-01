import uuid

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

WEBHOOK_SECRET = "test-alertmanager-secret"


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["ALERTMANAGER_WEBHOOK_SECRET"] = WEBHOOK_SECRET
    with _app.app_context():
        yield _app


@pytest.fixture
def client(app):
    return app.test_client()


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
    a = Application(
        name="webapp",
        slug="webapp",
        project_id=project.id,
    )
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


def _auth_headers():
    return {"Authorization": f"Bearer {WEBHOOK_SECRET}"}


def _alertmanager_payload(alerts, receiver="cabotage", status="firing"):
    return {
        "version": "4",
        "receiver": receiver,
        "status": status,
        "alerts": alerts,
        "groupLabels": {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://localhost:8080/alertmanager",
        "groupKey": '{}:{alertname="test"}',
        "truncatedAlerts": 0,
    }


def _oom_alert(deployment="test-app", namespace="cabotage", status="firing"):
    return {
        "status": status,
        "labels": {
            "alertname": "ResidentDeploymentOOMKilled",
            "deployment": deployment,
            "namespace": namespace,
            "severity": "critical",
        },
        "annotations": {
            "description": f"A container in deployment {deployment} was terminated due to OOM.",
            "summary": f"Deployment {namespace}/{deployment} has OOM killed pods",
        },
        "startsAt": "2026-03-30T17:57:58.931Z",
        "endsAt": (
            "0001-01-01T00:00:00Z" if status == "firing" else "2026-03-30T18:01:08.931Z"
        ),
        "generatorURL": "/graph?g0.expr=test",
        "fingerprint": uuid.uuid4().hex[:16],
    }


def _traefik_alert(service, status="firing", alertname="TraefikHighErrorRate"):
    return {
        "status": status,
        "labels": {
            "alertname": alertname,
            "service": service,
            "severity": "critical",
        },
        "annotations": {
            "description": f"Router {service} has a high 5xx error rate.",
            "summary": f"High 5xx error rate on {service}",
        },
        "startsAt": "2026-03-30T16:42:28.887Z",
        "endsAt": (
            "0001-01-01T00:00:00Z" if status == "firing" else "2026-03-30T16:45:28.887Z"
        ),
        "generatorURL": "/graph?g0.expr=test",
        "fingerprint": uuid.uuid4().hex[:16],
    }


def _slug_label_alert(org_slug, project_slug, app_slug, status="firing"):
    return {
        "status": status,
        "labels": {
            "alertname": "ResidentDeploymentOOMKilled",
            "label_organization": org_slug,
            "label_project": project_slug,
            "label_application": app_slug,
            "severity": "critical",
        },
        "annotations": {
            "description": "OOM killed.",
            "summary": "OOM killed.",
        },
        "startsAt": "2026-03-30T17:57:58.931Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "/graph?g0.expr=test",
        "fingerprint": uuid.uuid4().hex[:16],
    }


# --- Auth tests ---


class TestWebhookAuth:
    def test_rejects_missing_auth(self, client, db_session):
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([]),
        )
        assert resp.status_code == 403

    def test_rejects_wrong_token(self, client, db_session):
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([]),
            headers={"Authorization": "Bearer wrong-secret"},
        )
        assert resp.status_code == 403

    def test_rejects_non_bearer(self, client, db_session):
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([]),
            headers={"Authorization": f"Basic {WEBHOOK_SECRET}"},
        )
        assert resp.status_code == 403

    def test_accepts_valid_token(self, client, db_session):
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.get_json()["alerts_processed"] == 0

    def test_rejects_when_secret_not_configured(self, client, db_session):
        _app.config["ALERTMANAGER_WEBHOOK_SECRET"] = None
        try:
            resp = client.post(
                "/alertmanager/webhooks",
                json=_alertmanager_payload([]),
                headers=_auth_headers(),
            )
            assert resp.status_code == 403
        finally:
            _app.config["ALERTMANAGER_WEBHOOK_SECRET"] = WEBHOOK_SECRET


# --- Payload handling ---


class TestPayloadHandling:
    def test_rejects_non_json(self, client, db_session):
        resp = client.post(
            "/alertmanager/webhooks",
            data="not json",
            headers=_auth_headers(),
            content_type="text/plain",
        )
        assert resp.status_code == 400

    def test_empty_alerts_array(self, client, db_session):
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.get_json()["alerts_processed"] == 0

    def test_stores_single_alert(self, client, db_session):
        alert_data = _oom_alert()
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.get_json()["alerts_processed"] == 1

        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert is not None
        assert alert.alertname == "ResidentDeploymentOOMKilled"
        assert alert.status == "firing"
        assert alert.labels["severity"] == "critical"
        assert alert.annotations["description"].startswith("A container in deployment")

    def test_stores_multiple_alerts(self, client, db_session):
        alerts = [_oom_alert() for _ in range(3)]
        fingerprints = [a["fingerprint"] for a in alerts]
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload(alerts),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.get_json()["alerts_processed"] == 3
        stored = Alert.query.filter(Alert.fingerprint.in_(fingerprints)).count()
        assert stored == 3

    def test_stores_group_key_and_receiver(self, client, db_session):
        alert_data = _oom_alert()
        payload = _alertmanager_payload([alert_data], receiver="my-receiver")
        payload["groupKey"] = '{}:{alertname="ResidentDeploymentOOMKilled"}'
        resp = client.post(
            "/alertmanager/webhooks",
            json=payload,
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.receiver == "my-receiver"
        assert alert.group_key == '{}:{alertname="ResidentDeploymentOOMKilled"}'

    def test_resolved_alert_has_ends_at(self, client, db_session):
        alert_data = _oom_alert(status="resolved")
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data], status="resolved"),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.status == "resolved"
        assert alert.ends_at is not None

    def test_firing_alert_has_null_ends_at(self, client, db_session):
        alert_data = _oom_alert(status="firing")
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.status == "firing"
        assert alert.ends_at is None

    def test_skips_alert_missing_starts_at(self, client, db_session):
        alert_data = _oom_alert()
        del alert_data["startsAt"]
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.get_json()["alerts_processed"] == 0

    def test_stores_generator_url(self, client, db_session):
        alert_data = _oom_alert()
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.generator_url == "/graph?g0.expr=test"


# --- Resolution by slug labels ---


class TestResolveBySlugLabels:
    def test_resolves_by_slug_labels(
        self, client, db_session, org, project, application, app_env
    ):
        alert_data = _slug_label_alert(org.slug, project.slug, application.slug)
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.application_id == application.id
        assert alert.application_environment_id == app_env.id

    def test_no_match_wrong_slug(
        self, client, db_session, org, project, application, app_env
    ):
        alert_data = _slug_label_alert(org.slug, project.slug, "nonexistent")
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.application_id is None
        assert alert.application_environment_id is None


# --- Resolution by deployment name ---


class TestResolveByDeployment:
    def test_resolves_by_deployment_and_namespace(
        self, client, db_session, org, project, application, app_env
    ):
        deployment_name = f"{project.k8s_identifier}-{application.k8s_identifier}"
        alert_data = _oom_alert(
            deployment=deployment_name, namespace=org.k8s_identifier
        )
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.application_id == application.id
        assert alert.application_environment_id == app_env.id

    def test_resolves_by_deployment_without_namespace(
        self, client, db_session, org, project, application, app_env
    ):
        deployment_name = f"{project.k8s_identifier}-{application.k8s_identifier}"
        alert_data = _oom_alert(deployment=deployment_name, namespace="cabotage")
        # Remove the namespace to test without it
        del alert_data["labels"]["namespace"]
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.application_id == application.id

    def test_no_match_wrong_deployment(
        self, client, db_session, org, project, application, app_env
    ):
        alert_data = _oom_alert(
            deployment="nonexistent-app", namespace=org.k8s_identifier
        )
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.application_id is None

    def test_no_match_wrong_namespace(
        self, client, db_session, org, project, application, app_env
    ):
        deployment_name = f"{project.k8s_identifier}-{application.k8s_identifier}"
        alert_data = _oom_alert(deployment=deployment_name, namespace="wrong-namespace")
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.application_id is None


# --- Resolution by Traefik service ---


class TestResolveByTraefikService:
    def test_resolves_by_traefik_service(
        self, client, db_session, org, project, application, app_env
    ):
        resource_prefix = f"{project.k8s_identifier}-{application.k8s_identifier}"
        service = f"{org.k8s_identifier}-{resource_prefix}-web-somehostname-web-https@kubernetesingressnginx"
        alert_data = _traefik_alert(service=service)
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.application_id == application.id
        assert alert.application_environment_id == app_env.id

    def test_no_match_without_at_sign(
        self, client, db_session, org, project, application, app_env
    ):
        alert_data = _traefik_alert(service="no-at-sign-here")
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.application_id is None

    def test_no_match_wrong_service(
        self, client, db_session, org, project, application, app_env
    ):
        alert_data = _traefik_alert(
            service="totally-wrong-service-name@kubernetesingressnginx"
        )
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.application_id is None


# --- Resolution priority ---


class TestResolutionPriority:
    def test_slug_labels_take_precedence_over_deployment(
        self, client, db_session, org, project, application, app_env
    ):
        """When both slug labels and deployment name are present,
        slug labels should be used."""
        deployment_name = f"{project.k8s_identifier}-{application.k8s_identifier}"
        alert_data = {
            "status": "firing",
            "labels": {
                "alertname": "ResidentDeploymentOOMKilled",
                "label_organization": org.slug,
                "label_project": project.slug,
                "label_application": application.slug,
                "deployment": deployment_name,
                "namespace": org.k8s_identifier,
                "severity": "critical",
            },
            "annotations": {},
            "startsAt": "2026-03-30T17:57:58.931Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "/graph?g0.expr=test",
            "fingerprint": uuid.uuid4().hex[:16],
        }
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert.application_id == application.id

    def test_unresolvable_alert_still_stored(self, client, db_session):
        """Alerts that can't be resolved to an application should still be stored."""
        alert_data = {
            "status": "firing",
            "labels": {
                "alertname": "SomeInfraAlert",
                "severity": "warning",
            },
            "annotations": {"summary": "Something happened"},
            "startsAt": "2026-03-30T17:57:58.931Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "/graph?g0.expr=test",
            "fingerprint": uuid.uuid4().hex[:16],
        }
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.get_json()["alerts_processed"] == 1
        alert = Alert.query.filter_by(fingerprint=alert_data["fingerprint"]).first()
        assert alert is not None
        assert alert.application_id is None
        assert alert.alertname == "SomeInfraAlert"


# --- Upsert behavior ---


class TestUpsert:
    def test_refire_updates_existing_row(self, client, db_session):
        """Re-firing the same alert should update, not insert a second row."""
        fingerprint = uuid.uuid4().hex[:16]
        alert_data = {
            "status": "firing",
            "labels": {
                "alertname": "ResidentDeploymentOOMKilled",
                "deployment": "test-app",
                "namespace": "cabotage",
                "severity": "critical",
            },
            "annotations": {"summary": "OOM killed"},
            "startsAt": "2026-03-30T17:57:58.931Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "/graph?g0.expr=test",
            "fingerprint": fingerprint,
        }

        # First fire
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.get_json()["alerts_processed"] == 1
        count = Alert.query.filter_by(fingerprint=fingerprint).count()
        assert count == 1
        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        original_id = alert.id

        # Re-fire (same fingerprint + startsAt)
        resp = client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([alert_data]),
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        count = Alert.query.filter_by(fingerprint=fingerprint).count()
        assert count == 1
        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.id == original_id
        assert alert.status == "firing"

    def test_resolution_updates_status_and_ends_at(self, client, db_session):
        """Resolving an alert should update status and ends_at on the existing row."""
        fingerprint = uuid.uuid4().hex[:16]
        firing = {
            "status": "firing",
            "labels": {
                "alertname": "ResidentDeploymentOOMKilled",
                "deployment": "test-app",
                "namespace": "cabotage",
                "severity": "critical",
            },
            "annotations": {"summary": "OOM killed"},
            "startsAt": "2026-03-30T17:57:58.931Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "/graph?g0.expr=test",
            "fingerprint": fingerprint,
        }
        resolved = {
            **firing,
            "status": "resolved",
            "endsAt": "2026-03-30T18:10:00.000Z",
        }

        # Fire
        client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([firing]),
            headers=_auth_headers(),
        )
        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.status == "firing"
        assert alert.ends_at is None
        original_id = alert.id

        # Resolve
        client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([resolved], status="resolved"),
            headers=_auth_headers(),
        )
        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.id == original_id
        assert alert.status == "resolved"
        assert alert.ends_at is not None
        assert Alert.query.filter_by(fingerprint=fingerprint).count() == 1

    def test_same_fingerprint_new_starts_at_creates_new_row(self, client, db_session):
        """Same fingerprint but different startsAt means a new incident."""
        fingerprint = uuid.uuid4().hex[:16]
        first_incident = {
            "status": "resolved",
            "labels": {
                "alertname": "ResidentDeploymentOOMKilled",
                "deployment": "test-app",
                "namespace": "cabotage",
                "severity": "critical",
            },
            "annotations": {"summary": "OOM killed"},
            "startsAt": "2026-03-30T17:57:58.931Z",
            "endsAt": "2026-03-30T18:05:00.000Z",
            "generatorURL": "/graph?g0.expr=test",
            "fingerprint": fingerprint,
        }
        second_incident = {
            **first_incident,
            "status": "firing",
            "startsAt": "2026-03-30T19:00:00.000Z",
            "endsAt": "0001-01-01T00:00:00Z",
        }

        # First incident
        client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([first_incident]),
            headers=_auth_headers(),
        )
        # Second incident (new startsAt)
        client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([second_incident]),
            headers=_auth_headers(),
        )

        alerts = Alert.query.filter_by(fingerprint=fingerprint).all()
        assert len(alerts) == 2
        statuses = {a.status for a in alerts}
        assert statuses == {"resolved", "firing"}

    def test_upsert_backfills_application_resolution(
        self, client, db_session, org, project, application, app_env
    ):
        """If an alert initially can't be resolved but a later webhook can,
        the application should be backfilled."""
        fingerprint = uuid.uuid4().hex[:16]
        deployment_name = f"{project.k8s_identifier}-{application.k8s_identifier}"

        # First fire — no resolvable labels
        unresolvable = {
            "status": "firing",
            "labels": {
                "alertname": "ResidentDeploymentOOMKilled",
                "severity": "critical",
            },
            "annotations": {"summary": "OOM"},
            "startsAt": "2026-03-30T17:57:58.931Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "/graph?g0.expr=test",
            "fingerprint": fingerprint,
        }
        client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([unresolvable]),
            headers=_auth_headers(),
        )
        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.application_id is None

        # Re-fire with deployment labels
        resolvable = {
            **unresolvable,
            "labels": {
                **unresolvable["labels"],
                "deployment": deployment_name,
                "namespace": org.k8s_identifier,
            },
        }
        client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([resolvable]),
            headers=_auth_headers(),
        )
        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.application_id == application.id
        assert Alert.query.filter_by(fingerprint=fingerprint).count() == 1

    def test_upsert_does_not_clear_existing_resolution(
        self, client, db_session, org, project, application, app_env
    ):
        """If an alert was resolved to an app, a later webhook without labels
        should not clear the resolution."""
        fingerprint = uuid.uuid4().hex[:16]
        deployment_name = f"{project.k8s_identifier}-{application.k8s_identifier}"

        # Fire with resolvable labels
        resolvable = {
            "status": "firing",
            "labels": {
                "alertname": "ResidentDeploymentOOMKilled",
                "deployment": deployment_name,
                "namespace": org.k8s_identifier,
                "severity": "critical",
            },
            "annotations": {"summary": "OOM"},
            "startsAt": "2026-03-30T17:57:58.931Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "/graph?g0.expr=test",
            "fingerprint": fingerprint,
        }
        client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([resolvable]),
            headers=_auth_headers(),
        )
        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.application_id == application.id

        # Resolve without deployment labels
        resolved_no_labels = {
            **resolvable,
            "status": "resolved",
            "labels": {
                "alertname": "ResidentDeploymentOOMKilled",
                "severity": "critical",
            },
            "endsAt": "2026-03-30T18:10:00.000Z",
        }
        client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([resolved_no_labels], status="resolved"),
            headers=_auth_headers(),
        )
        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.status == "resolved"
        assert alert.application_id == application.id

    def test_resolved_alert_cannot_return_to_firing(self, client, db_session):
        """Once an alert is resolved, a new firing webhook with the same
        fingerprint+startsAt must not flip it back to firing."""
        fingerprint = uuid.uuid4().hex[:16]
        firing = {
            "status": "firing",
            "labels": {
                "alertname": "ResidentDeploymentOOMKilled",
                "deployment": "test-app",
                "namespace": "cabotage",
                "severity": "critical",
            },
            "annotations": {"summary": "OOM killed"},
            "startsAt": "2026-03-30T17:57:58.931Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "/graph?g0.expr=test",
            "fingerprint": fingerprint,
        }
        resolved = {
            **firing,
            "status": "resolved",
            "endsAt": "2026-03-30T18:10:00.000Z",
        }

        # Fire, then resolve
        client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([firing]),
            headers=_auth_headers(),
        )
        client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([resolved], status="resolved"),
            headers=_auth_headers(),
        )
        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.status == "resolved"

        # Try to re-fire — should be ignored
        client.post(
            "/alertmanager/webhooks",
            json=_alertmanager_payload([firing]),
            headers=_auth_headers(),
        )
        alert = Alert.query.filter_by(fingerprint=fingerprint).first()
        assert alert.status == "resolved"
        assert Alert.query.filter_by(fingerprint=fingerprint).count() == 1
