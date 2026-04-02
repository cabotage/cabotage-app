import uuid
from datetime import datetime, timedelta, UTC
from unittest.mock import patch

import pytest

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.notifications import (
    NotificationRoute,
    SentNotification,
)
from cabotage.server.models.projects import (
    Alert,
    Application,
    ApplicationEnvironment,
    Environment,
    Project,
)
from cabotage.server.wsgi import app as _app

from cabotage.celery.tasks.notify import (
    ALERTNAME_TYPE_MAP,
    _dispatch_alert_notification_impl,
    _dispatch_pipeline_notification_impl,
    format_alert_message,
    format_pipeline_message,
    resolve_routes,
    send_notification,
)


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["ALERTMANAGER_WEBHOOK_SECRET"] = "test-secret"
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


@pytest.fixture
def firing_alert(db_session, application, app_env):
    alert = Alert(
        fingerprint=f"abc-{uuid.uuid4().hex[:8]}",
        status="firing",
        alertname="ResidentDeploymentOOMKilled",
        labels={"alertname": "ResidentDeploymentOOMKilled", "severity": "critical"},
        annotations={"summary": "Pod OOM killed"},
        starts_at=datetime(2026, 3, 30, 17, 0, 0),
        ends_at=None,
        generator_url="http://prometheus/graph?g0.expr=test",
        application_id=application.id,
        application_environment_id=app_env.id,
    )
    db_session.add(alert)
    db_session.flush()
    return alert


@pytest.fixture
def resolved_alert(db_session, application, app_env):
    alert = Alert(
        fingerprint=f"def-{uuid.uuid4().hex[:8]}",
        status="resolved",
        alertname="ResidentDeploymentOOMKilled",
        labels={"alertname": "ResidentDeploymentOOMKilled", "severity": "critical"},
        annotations={"summary": "Pod OOM killed"},
        starts_at=datetime(2026, 3, 30, 17, 0, 0),
        ends_at=datetime(2026, 3, 30, 17, 12, 34),
        generator_url="http://prometheus/graph?g0.expr=test",
        application_id=application.id,
        application_environment_id=app_env.id,
    )
    db_session.add(alert)
    db_session.flush()
    return alert


# --- Alertname mapping ---


class TestAlertNameMapping:
    def test_oom_maps_to_health_oom(self):
        assert ALERTNAME_TYPE_MAP["ResidentDeploymentOOMKilled"] == "health.oom"

    def test_crash_loop_maps_to_health_crash_restart(self):
        assert (
            ALERTNAME_TYPE_MAP["ResidentDeploymentCrashLoop"] == "health.crash_restart"
        )

    def test_traefik_error_maps_to_http_5xx(self):
        assert ALERTNAME_TYPE_MAP["TraefikHighErrorRate"] == "http.5xx"

    def test_traefik_latency_maps_to_http_latency(self):
        assert ALERTNAME_TYPE_MAP["TraefikHighLatency"] == "http.latency"

    def test_unknown_alertname_not_in_map(self):
        assert "SomeRandomAlert" not in ALERTNAME_TYPE_MAP


# --- Message formatting ---


class TestFormatAlertMessage:
    def test_firing_message(self, db_session, firing_alert, application, app_env):
        result = format_alert_message(firing_alert, application, app_env)
        assert "slack_attachments" in result
        assert "discord_embeds" in result
        text = result["text"]
        assert "FIRING" in text
        assert "ResidentDeploymentOOMKilled" in text
        assert application.project.organization.slug in text
        # Slack attachment has color bar
        assert result["slack_attachments"][0]["color"] == "#e74c3c"
        # Discord embed has color
        assert result["discord_embeds"][0]["color"] == 0xE74C3C

    def test_resolved_message(self, db_session, resolved_alert, application, app_env):
        result = format_alert_message(resolved_alert, application, app_env)
        assert "RESOLVED" in result["text"]
        assert "12m 34s" in result["text"]
        assert result["slack_attachments"][0]["color"] == "#2ecc71"
        assert result["discord_embeds"][0]["color"] == 0x2ECC71

    def test_message_without_application(self, db_session, firing_alert):
        result = format_alert_message(firing_alert, None, None)
        assert "FIRING" in result["text"]
        assert "ResidentDeploymentOOMKilled" in result["text"]


class TestFormatPipelineMessage:
    def test_started_message(self):
        result = format_pipeline_message(
            "pipeline.image_build",
            "myorg / myproj / myapp",
            "Triggered by: alice",
        )
        assert "Image build started" in result["text"]
        assert result["slack_attachments"][0]["color"] == "#3498db"

    def test_failure_message(self):
        result = format_pipeline_message(
            "pipeline.deploy",
            "myorg / myproj / myapp",
            "Triggered by: alice",
            error="Timeout waiting for pods",
        )
        assert "Deploy failed" in result["text"]
        assert result["slack_attachments"][0]["color"] == "#e74c3c"

    def test_complete_message(self):
        result = format_pipeline_message(
            "pipeline.release",
            "myorg / myproj / myapp",
            "Triggered by: bob",
            complete=True,
        )
        assert "Release complete" in result["text"]
        assert result["slack_attachments"][0]["color"] == "#2ecc71"


# --- Route resolution ---


class TestResolveRoutes:
    def test_returns_matching_route(
        self, db_session, org, project, application, app_env
    ):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["health.oom"],
            integration="slack",
            channel_id="C001",
            channel_name="#alerts",
            enabled=True,
        )
        db_session.add(route)
        db_session.flush()

        targets = resolve_routes(org, "health.oom", application, app_env)
        assert ("slack", "C001") in targets

    def test_skips_disabled_route(self, db_session, org, project, application, app_env):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["health.oom"],
            integration="slack",
            channel_id="C001",
            channel_name="#alerts",
            enabled=False,
        )
        db_session.add(route)
        db_session.flush()

        targets = resolve_routes(org, "health.oom", application, app_env)
        assert ("slack", "C001") not in targets

    def test_skips_wrong_type(self, db_session, org, project, application, app_env):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["http.5xx"],
            integration="slack",
            channel_id="C001",
            channel_name="#alerts",
            enabled=True,
        )
        db_session.add(route)
        db_session.flush()

        targets = resolve_routes(org, "health.oom", application, app_env)
        assert ("slack", "C001") not in targets

    def test_project_scoped_route_matches(
        self, db_session, org, project, application, app_env
    ):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["health.oom"],
            project_ids=[str(project.id)],
            integration="slack",
            channel_id="C002",
            channel_name="#project-alerts",
            enabled=True,
        )
        db_session.add(route)
        db_session.flush()

        targets = resolve_routes(org, "health.oom", application, app_env)
        assert ("slack", "C002") in targets

    def test_project_scoped_route_skips_wrong_project(
        self, db_session, org, project, application, app_env
    ):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["health.oom"],
            project_ids=[str(uuid.uuid4())],
            integration="slack",
            channel_id="C002",
            channel_name="#project-alerts",
            enabled=True,
        )
        db_session.add(route)
        db_session.flush()

        targets = resolve_routes(org, "health.oom", application, app_env)
        assert ("slack", "C002") not in targets

    def test_application_scoped_route_matches(
        self, db_session, org, project, application, app_env
    ):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["health.oom"],
            application_ids=[str(application.id)],
            integration="discord",
            channel_id="D001",
            channel_name="alerts",
            enabled=True,
        )
        db_session.add(route)
        db_session.flush()

        targets = resolve_routes(org, "health.oom", application, app_env)
        assert ("discord", "D001") in targets

    def test_deduplicates_routes(self, db_session, org, project, application, app_env):
        for _ in range(3):
            route = NotificationRoute(
                organization_id=org.id,
                notification_types=["health.oom"],
                integration="slack",
                channel_id="C001",
                channel_name="#alerts",
                enabled=True,
            )
            db_session.add(route)
        db_session.flush()

        targets = resolve_routes(org, "health.oom", application, app_env)
        assert targets.count(("slack", "C001")) == 1

    def test_falls_back_to_default_channel(
        self, db_session, org, project, application, app_env
    ):
        from cabotage.server.models.auth import SlackIntegration

        slack = SlackIntegration(
            organization_id=org.id,
            team_id="T001",
            default_channel_id="C_DEFAULT",
            default_channel_name="#general",
        )
        db_session.add(slack)
        db_session.flush()

        targets = resolve_routes(org, "health.oom", application, app_env)
        assert ("slack", "C_DEFAULT") in targets

    def test_no_targets_when_no_routes_and_no_integrations(
        self, db_session, org, project, application, app_env
    ):
        targets = resolve_routes(org, "health.oom", application, app_env)
        assert targets == []


# --- send_notification task ---


class TestSendNotification:
    def test_sends_and_tracks_new_message(self, db_session, org, application, app_env):
        with (
            patch(
                "cabotage.celery.tasks.notify._send_discord_message",
                return_value="msg-123",
            ) as mock_send,
            patch("cabotage.celery.tasks.notify.Organization.query") as mock_org_q,
            patch("cabotage.celery.tasks.notify.db.session.commit"),
            patch("cabotage.celery.tasks.notify.db.session.add") as mock_add,
        ):
            mock_org_q.filter_by.return_value.first.return_value = org
            send_notification(
                str(org.id),
                "discord",
                "D001",
                "Alert",
                str(application.id),
                "health.oom",
                "test message",
            )
            mock_send.assert_called_once_with(
                "D001", "test message", embeds=None, components=None
            )
            # Verify a SentNotification was added to the session
            assert mock_add.called
            sent = mock_add.call_args[0][0]
            assert isinstance(sent, SentNotification)
            assert sent.external_message_id == "msg-123"
            assert sent.integration == "discord"
            assert sent.channel_id == "D001"

    def test_updates_existing_message(self, db_session, org, application, app_env):
        sent = SentNotification(
            organization_id=org.id,
            notification_type="health.oom",
            object_type="Alert",
            object_id=application.id,
            integration="discord",
            channel_id="D001",
            external_message_id="msg-123",
        )
        db_session.add(sent)
        db_session.flush()

        with (
            patch(
                "cabotage.celery.tasks.notify._update_discord_message"
            ) as mock_update,
            patch("cabotage.celery.tasks.notify.Organization.query") as mock_org_q,
            patch("cabotage.celery.tasks.notify.SentNotification.query") as mock_sent_q,
            patch("cabotage.celery.tasks.notify.db.session.commit"),
        ):
            mock_org_q.filter_by.return_value.first.return_value = org
            mock_sent_q.filter_by.return_value.first.return_value = sent
            send_notification(
                str(org.id),
                "discord",
                "D001",
                "Alert",
                str(application.id),
                "health.oom",
                "updated message",
            )
            mock_update.assert_called_once_with(
                "D001", "msg-123", "updated message", embeds=None, components=None
            )

    def test_raises_on_send_failure(self, db_session, org, application, app_env):
        from cabotage.server.models.auth import DiscordIntegration

        discord = DiscordIntegration(
            organization_id=org.id,
            guild_id="G001",
        )
        db_session.add(discord)
        db_session.flush()

        with (
            patch(
                "cabotage.celery.tasks.notify._send_discord_message",
                return_value=None,
            ),
            patch("cabotage.celery.tasks.notify.Organization.query") as mock_org_q,
            patch("cabotage.celery.tasks.notify.db.session.commit"),
            pytest.raises(RuntimeError, match="Failed to send discord"),
        ):
            mock_org_q.filter_by.return_value.first.return_value = org
            send_notification(
                str(org.id),
                "discord",
                "D001",
                "Alert",
                str(application.id),
                "health.oom",
                "test",
            )


# --- Cooldown ---


class TestCooldown:
    def test_skips_notification_within_cooldown(self, db_session, firing_alert, org):
        firing_alert.last_notified_at = datetime.now(UTC).replace(tzinfo=None)
        db_session.flush()

        with patch.object(send_notification, "delay") as mock_delay:
            _dispatch_alert_notification_impl(str(firing_alert.id))
            mock_delay.assert_not_called()

    def test_sends_notification_after_cooldown(
        self, db_session, firing_alert, org, application, app_env
    ):
        firing_alert.last_notified_at = datetime.now(UTC).replace(
            tzinfo=None
        ) - timedelta(minutes=10)
        db_session.flush()

        from cabotage.server.models.auth import SlackIntegration

        slack = SlackIntegration(
            organization_id=org.id,
            team_id="T001",
            default_channel_id="C_DEFAULT",
            default_channel_name="#general",
        )
        db_session.add(slack)
        db_session.flush()

        with patch.object(send_notification, "delay") as mock_delay:
            _dispatch_alert_notification_impl(str(firing_alert.id))
            assert mock_delay.called

    def test_always_sends_on_resolution(
        self, db_session, resolved_alert, org, application, app_env
    ):
        resolved_alert.last_notified_at = datetime.now(UTC).replace(tzinfo=None)
        db_session.flush()

        from cabotage.server.models.auth import SlackIntegration

        slack = SlackIntegration(
            organization_id=org.id,
            team_id="T001",
            default_channel_id="C_DEFAULT",
            default_channel_name="#general",
        )
        db_session.add(slack)
        db_session.flush()

        with patch.object(send_notification, "delay") as mock_delay:
            _dispatch_alert_notification_impl(str(resolved_alert.id))
            assert mock_delay.called


# --- Dispatch integration ---


class TestDispatchAlertNotification:
    def test_dispatches_for_firing_alert(
        self, db_session, org, project, application, app_env, firing_alert
    ):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["health.oom"],
            integration="discord",
            channel_id="D001",
            channel_name="alerts",
            enabled=True,
        )
        db_session.add(route)
        db_session.flush()

        with patch.object(send_notification, "delay") as mock_delay:
            _dispatch_alert_notification_impl(str(firing_alert.id))
            mock_delay.assert_called_once()
            args = mock_delay.call_args[0]
            assert args[1] == "discord"
            assert args[2] == "D001"
            message = args[6]
            assert "FIRING" in message["text"]
            assert message.get("discord_embeds")

    def test_skips_alert_without_application(self, db_session, org):
        alert = Alert(
            fingerprint="orphan123",
            status="firing",
            alertname="SomeInfraAlert",
            labels={"alertname": "SomeInfraAlert"},
            annotations={},
            starts_at=datetime(2026, 3, 30, 17, 0, 0),
        )
        db_session.add(alert)
        db_session.flush()

        with patch.object(send_notification, "delay") as mock_delay:
            _dispatch_alert_notification_impl(str(alert.id))
            mock_delay.assert_not_called()

    def test_uncategorized_alert_goes_to_default_channel(
        self, db_session, org, project, application, app_env
    ):
        from cabotage.server.models.auth import SlackIntegration

        slack = SlackIntegration(
            organization_id=org.id,
            team_id="T001",
            default_channel_id="C_DEFAULT",
            default_channel_name="#general",
        )
        db_session.add(slack)

        alert = Alert(
            fingerprint="unknown123",
            status="firing",
            alertname="UnknownAlert",
            labels={"alertname": "UnknownAlert", "severity": "warning"},
            annotations={},
            starts_at=datetime(2026, 3, 30, 17, 0, 0),
            application_id=application.id,
            application_environment_id=app_env.id,
        )
        db_session.add(alert)
        db_session.flush()

        with patch.object(send_notification, "delay") as mock_delay:
            _dispatch_alert_notification_impl(str(alert.id))
            assert mock_delay.called
            assert mock_delay.call_args[0][2] == "C_DEFAULT"


class TestDispatchPipelineNotification:
    def test_dispatches_pipeline_event(
        self, db_session, org, project, application, app_env
    ):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["pipeline.image_build"],
            integration="slack",
            channel_id="C_BUILDS",
            channel_name="#builds",
            enabled=True,
        )
        db_session.add(route)
        db_session.flush()

        with patch.object(send_notification, "delay") as mock_delay:
            _dispatch_pipeline_notification_impl(
                "pipeline.image_build",
                "Image",
                str(uuid.uuid4()),
                str(org.id),
                str(application.id),
                str(app_env.id),
                detail="Triggered by: alice",
            )
            mock_delay.assert_called_once()
            assert "Image build started" in mock_delay.call_args[0][6]["text"]

    def test_dispatches_pipeline_failure(
        self, db_session, org, project, application, app_env
    ):
        route = NotificationRoute(
            organization_id=org.id,
            notification_types=["pipeline.deploy"],
            integration="discord",
            channel_id="D_DEPLOYS",
            channel_name="deploys",
            enabled=True,
        )
        db_session.add(route)
        db_session.flush()

        with patch.object(send_notification, "delay") as mock_delay:
            _dispatch_pipeline_notification_impl(
                "pipeline.deploy",
                "Deployment",
                str(uuid.uuid4()),
                str(org.id),
                str(application.id),
                str(app_env.id),
                error="Timeout waiting for pods",
            )
            mock_delay.assert_called_once()
            assert "Deploy failed" in mock_delay.call_args[0][6]["text"]
