"""Behavior of the Logging editor over the existing configuration store."""

import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from flask import template_rendered
from flask_security import hash_password
import requests

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Configuration,
    Deployment,
    Environment,
    EnvironmentConfigSubscription,
    EnvironmentConfiguration,
    Project,
)
from cabotage.server.user import views
from cabotage.server.wsgi import app as _app
from cabotage.utils.datadog import DATADOG_SITES, logging_configuration


@pytest.fixture
def logging_editor(monkeypatch):
    for key, value in {
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "REQUIRE_MFA": False,
    }.items():
        monkeypatch.setitem(_app.config, key, value)
    with _app.app_context(), patch.object(db.session, "commit", db.session.flush):
        try:
            user = User(
                username=f"logging-{uuid.uuid4().hex[:8]}",
                email=f"logging-{uuid.uuid4().hex[:8]}@example.com",
                password=hash_password("password123"),
                active=True,
                fs_uniquifier=uuid.uuid4().hex,
            )
            org = Organization(
                name="Logging tests", slug=f"logs-{uuid.uuid4().hex[:8]}"
            )
            db.session.add_all([user, org])
            db.session.flush()
            member = OrganizationMember(
                organization_id=org.id, user_id=user.id, admin=True
            )
            project = Project(name="Logging project", organization_id=org.id)
            db.session.add_all([member, project])
            db.session.flush()
            env = Environment(name="default", project_id=project.id, ephemeral=False)
            app = Application(name="web", slug="web", project_id=project.id)
            db.session.add_all([env, app])
            db.session.flush()
            app_env = ApplicationEnvironment(
                application_id=app.id, environment_id=env.id, k8s_identifier=None
            )
            db.session.add(app_env)
            db.session.flush()
            client = _app.test_client()
            with client.session_transaction() as session:
                session.update(
                    {
                        "_user_id": user.fs_uniquifier,
                        "_fresh": True,
                        "fs_cc": "set",
                        "fs_paa": time.time(),
                        "identity.id": user.id,
                        "identity.auth_type": "session",
                    }
                )
            writes = []

            def write_configuration(namespace, prefix, config):
                writes.append(
                    {
                        "name": config.name,
                        "value": config.value,
                        "secret": config.secret,
                        "buildtime": config.buildtime,
                        "app_env_id": config.application_environment_id,
                    }
                )
                return {
                    "config_key_slug": f"vault:test/{config.name}/{len(writes)}",
                    "build_key_slug": None,
                }

            write = Mock(side_effect=write_configuration)
            read = Mock(side_effect=AssertionError("Unexpected secret read"))
            network = Mock(side_effect=AssertionError("Unexpected network call"))
            monkeypatch.setattr(views.config_writer, "write_configuration", write)
            monkeypatch.setattr(views.config_writer, "read", read)
            monkeypatch.setattr(views.requests_lib, "post", network)
            yield SimpleNamespace(
                client=client,
                app=app,
                app_env=app_env,
                member=member,
                url=f"/projects/{org.slug}/{project.slug}/applications/{app.slug}/logging",
                write=write,
                writes=writes,
                read=read,
                network=network,
            )
        finally:
            db.session.rollback()


def _config(editor, name, value, *, shared=False, app_env=None, **kwargs):
    app_env = app_env or editor.app_env
    if shared:
        config = EnvironmentConfiguration(
            project_id=editor.app.project_id,
            environment_id=app_env.environment_id,
            name=name,
            value=value,
            **kwargs,
        )
        db.session.add(config)
        db.session.flush()
        db.session.add(
            EnvironmentConfigSubscription(
                application_environment_id=app_env.id,
                environment_configuration_id=config.id,
            )
        )
    else:
        config = Configuration(
            application_id=editor.app.id,
            application_environment_id=app_env.id,
            name=name,
            value=value,
            **kwargs,
        )
        db.session.add(config)
    db.session.flush()
    db.session.expire_all()
    return config


def _request(editor, *, data=None, url=None):
    db.session.expire_all()
    contexts = []

    def capture(sender, template, context, **extra):
        contexts.append(context)

    with template_rendered.connected_to(capture, _app):
        response = (
            editor.client.get(url or editor.url)
            if data is None
            else editor.client.post(url or editor.url, data=data)
        )
    return response, contexts[-1] if contexts else None


def _save(editor, **data):
    return _request(editor, data={"action": "save", **data})


def _local(editor, name, app_env=None):
    return Configuration.query.filter_by(
        application_environment_id=(app_env or editor.app_env).id, name=name
    ).first()


def test_variables_and_logging_share_values_and_versions(logging_editor):
    editor = logging_editor
    response = editor.client.post(
        editor.url.removesuffix("logging") + "config/bulk",
        data={
            "raw_text": "DD_LOGS_ENABLED=false\nDD_SITE=datadoghq.eu\nUNRELATED=keep",
        },
    )
    assert response.status_code == 302
    response, context = _request(editor)
    assert response.status_code == 200
    assert context["form"].site.data == "datadoghq.eu"
    assert context["form"].enabled.data == "false"
    site = _local(editor, "DD_SITE")
    version = site.version_id
    editor.writes.clear()
    response, _ = _save(editor, enabled="false", site="us3.datadoghq.com")
    assert response.status_code == 302
    assert _local(editor, "DD_SITE").value == "us3.datadoghq.com"
    assert _local(editor, "DD_SITE").version_id > version
    assert _local(editor, "UNRELATED").value == "keep"
    assert [write["name"] for write in editor.writes] == ["DD_SITE"]
    variables = editor.client.get(editor.url.removesuffix("logging") + "config")
    assert "us3.datadoghq.com" in variables.get_data(as_text=True)


def test_empty_save_does_not_create_settings(logging_editor):
    editor = logging_editor
    response, _ = _save(editor, enabled="false", site="", api_key="")
    assert response.status_code == 302
    editor.write.assert_not_called()
    assert _local(editor, "DD_LOGS_ENABLED") is None


def test_credentials_alone_do_not_enable_export(logging_editor):
    editor = logging_editor
    response, _ = _save(editor, site="datadoghq.com", api_key="new-test-key")
    assert response.status_code == 302
    assert _local(editor, "DD_LOGS_ENABLED") is None
    key = _local(editor, "DD_API_KEY")
    assert key.secret and not key.buildtime
    assert key.value == "**secure**"
    assert editor.writes[-1]["value"] == "new-test-key"


def test_enabling_preserves_untouched_secret_and_flags(logging_editor):
    editor = logging_editor
    _config(editor, "DD_SITE", "datadoghq.com", buildtime=True)
    key = _config(
        editor,
        "DD_API_KEY",
        "**secure**",
        secret=True,
        buildtime=True,
        key_slug="vault:tenant/key",
    )
    key_version = key.version_id
    editor.read.side_effect = None
    editor.read.return_value = {"data": {"DD_API_KEY": "saved-test-key"}}
    response, _ = _save(editor, enabled="true", site="datadoghq.com", api_key="")
    assert response.status_code == 302
    assert [write["name"] for write in editor.writes] == ["DD_LOGS_ENABLED"]
    assert key.version_id == key_version
    assert key.secret and key.buildtime
    assert _local(editor, "DD_SITE").buildtime
    editor.read.assert_called_once_with("tenant/key", secret=True)


def test_secret_never_read_or_rendered_on_get(logging_editor):
    editor = logging_editor
    for name in ("DD_LOGS_ENABLED", "DD_SITE", "DD_API_KEY"):
        _config(
            editor,
            name,
            "do-not-render-this-key",
            secret=True,
            buildtime=True,
            key_slug="vault:tenant/key",
        )
    response, context = _request(editor)
    assert response.status_code == 200
    assert "do-not-render-this-key" not in response.get_data(as_text=True)
    assert context["form"].api_key.data is None
    editor.read.assert_not_called()


def test_plaintext_api_key_is_never_rendered_and_replacement_is_secure(logging_editor):
    editor = logging_editor
    key = _config(editor, "DD_API_KEY", "plain-test-key", buildtime=True)
    response, context = _request(editor)
    assert "plain-test-key" not in response.get_data(as_text=True)
    assert context["logging_fields"]["api_key"]["issues"]
    response, _ = _save(editor, api_key="plain-test-key")
    assert response.status_code == 302
    assert key.secret and not key.buildtime and key.value == "**secure**"


@pytest.mark.parametrize(
    "field,name,value,replacement",
    [
        ("enabled", "DD_LOGS_ENABLED", "false", "false"),
        ("site", "DD_SITE", "datadoghq.com", "datadoghq.eu"),
        ("api_key", "DD_API_KEY", "**secure**", "replacement-test-key"),
    ],
)
def test_inherited_edits_require_explicit_override(
    logging_editor, field, name, value, replacement
):
    editor = logging_editor
    shared = _config(
        editor,
        name,
        value,
        shared=True,
        secret=field == "api_key",
        key_slug="vault:shared/key",
    )
    version = shared.version_id
    # An explicit same-value boolean override must still create a local variable.
    attempted = "true" if field == "enabled" else replacement
    response, context = _save(editor, **{field: attempted})
    assert response.status_code == 200
    assert context["form"].errors[field]
    editor.write.assert_not_called()
    assert _local(editor, name) is None
    response, _ = _save(editor, **{field: replacement, f"override_{field}": "y"})
    assert response.status_code == 302
    assert _local(editor, name) is not None
    assert shared.value == value and shared.version_id == version
    _, context = _request(editor)
    assert context["logging_fields"][field]["source"] == "application"


def test_untouched_shared_values_stay_shared(logging_editor):
    editor = logging_editor
    _config(editor, "DD_SITE", "datadoghq.eu", shared=True)
    _config(
        editor,
        "DD_API_KEY",
        "**secure**",
        shared=True,
        secret=True,
        key_slug="vault:shared/key",
    )
    response, _ = _save(editor, enabled="false")
    assert response.status_code == 302
    editor.write.assert_not_called()
    assert _local(editor, "DD_SITE") is None
    assert _local(editor, "DD_API_KEY") is None


def test_shared_secret_override_requires_replacement(logging_editor):
    editor = logging_editor
    _config(editor, "DD_API_KEY", "**secure**", shared=True, secret=True)
    response, context = _save(editor, override_api_key="y", api_key="")
    assert response.status_code == 200
    assert context["form"].errors["api_key"]
    editor.write.assert_not_called()
    editor.read.assert_not_called()


@pytest.mark.parametrize(
    "value", ["**secure**", "**secret**", "********", "{{TOKEN}}", "a\nb"]
)
def test_masked_or_invalid_keys_are_not_written(logging_editor, value):
    editor = logging_editor
    response, context = _save(editor, api_key=value)
    assert response.status_code == 200
    assert context["form"].errors["api_key"]
    assert context["form"].api_key.data is None
    editor.write.assert_not_called()


def test_invalid_manual_values_are_visible_and_can_be_disabled(logging_editor):
    editor = logging_editor
    _config(editor, "DD_LOGS_ENABLED", "manually-invalid")
    site = _config(editor, "DD_SITE", "https://invalid.example")
    _, context = _request(editor)
    assert context["form"].enabled.data == "manually-invalid"
    assert context["form"].site.data == "https://invalid.example"
    assert context["logging_fields"]["enabled"]["issues"]
    assert context["logging_fields"]["site"]["issues"]
    response, _ = _save(editor, enabled="false", site=site.value)
    assert response.status_code == 302
    assert _local(editor, "DD_LOGS_ENABLED").value == "false"
    assert [write["name"] for write in editor.writes] == ["DD_LOGS_ENABLED"]
    editor.read.assert_not_called()


def test_disabled_export_does_not_validate_broken_secret(logging_editor):
    editor = logging_editor
    _config(editor, "DD_LOGS_ENABLED", "true")
    _config(editor, "DD_SITE", "invalid")
    _config(editor, "DD_API_KEY", "**secure**", secret=True)
    response, _ = _save(editor, enabled="false", site="invalid")
    assert response.status_code == 302
    editor.read.assert_not_called()


@pytest.mark.parametrize(
    "site,key",
    [(None, None), ("invalid", "valid-test-key"), ("datadoghq.com", "**secure**")],
)
def test_enabling_requires_valid_saved_destination(logging_editor, site, key):
    editor = logging_editor
    if site is not None:
        _config(editor, "DD_SITE", site)
    if key is not None:
        _config(editor, "DD_API_KEY", key)
    response, context = _save(editor, enabled="true")
    assert response.status_code == 200
    assert context["form"].errors
    editor.write.assert_not_called()


def test_read_only_members_cannot_save_or_test(logging_editor):
    editor = logging_editor
    editor.member.admin = False
    db.session.flush()
    response, context = _request(editor)
    assert response.status_code == 200 and not context["can_edit"]
    for action in ("save", "test"):
        response, _ = _request(editor, data={"action": action, "enabled": "true"})
        assert response.status_code == 403
    editor.write.assert_not_called()
    editor.network.assert_not_called()


def test_nonmembers_cannot_view_logging(logging_editor):
    editor = logging_editor
    db.session.delete(editor.member)
    db.session.flush()
    response, _ = _request(editor)
    assert response.status_code == 403


@pytest.mark.parametrize("action", ["save", "test"])
def test_csrf_required_for_mutations(logging_editor, monkeypatch, action):
    editor = logging_editor
    monkeypatch.setitem(_app.config, "WTF_CSRF_ENABLED", True)
    response, _ = _request(editor, data={"action": action, "enabled": "true"})
    assert response.status_code == 400
    editor.write.assert_not_called()
    editor.network.assert_not_called()


def test_environment_scoped_settings_and_redirect(logging_editor):
    editor = logging_editor
    project = editor.app.project
    project.environments_enabled = True
    env = Environment(name="production", slug="production", project_id=project.id)
    db.session.add(env)
    db.session.flush()
    other = ApplicationEnvironment(application_id=editor.app.id, environment_id=env.id)
    db.session.add(other)
    db.session.flush()
    original = _config(editor, "DD_SITE", "datadoghq.com")
    _config(editor, "DD_SITE", "datadoghq.eu", app_env=other)
    url = editor.url + "?env_slug=production"
    _, context = _request(editor, url=url)
    assert context["form"].site.data == "datadoghq.eu"
    response, _ = _request(
        editor,
        url=url,
        data={
            "action": "save",
            "site": "us3.datadoghq.com",
            "environment_id": str(editor.app_env.environment_id),
        },
    )
    assert response.status_code == 302
    assert response.location.endswith("?env_slug=production")
    assert original.value == "datadoghq.com"
    assert _local(editor, "DD_SITE", other).value == "us3.datadoghq.com"
    assert editor.writes[0]["app_env_id"] == other.id
    response, _ = _request(editor, url=editor.url + "?env_slug=missing")
    assert response.status_code == 404


def _deployment(editor, *, complete=True, error=False):
    db.session.expire_all()
    deployment = Deployment(
        application_id=editor.app.id,
        application_environment_id=editor.app_env.id,
        release={"configuration": editor.app._resolved_configuration(editor.app_env)},
        complete=complete,
        error=error,
    )
    db.session.add(deployment)
    db.session.flush()
    return deployment


def test_status_tracks_snapshot_versions_not_enabled_flag(logging_editor):
    editor = logging_editor
    _config(editor, "DD_LOGS_ENABLED", "true")
    _config(editor, "DD_SITE", "datadoghq.com")
    _, context = _request(editor)
    assert context["logging_status"]["state"] == "not_deployed"
    _deployment(editor)
    _, context = _request(editor)
    assert context["logging_status"]["state"] == "deployed"
    response, _ = _save(editor, enabled="false")
    assert response.status_code == 302
    _, context = _request(editor)
    assert context["logging_status"]["state"] == "pending"
    _deployment(editor, complete=False, error=True)
    _, context = _request(editor)
    assert context["logging_status"]["state"] == "pending"
    _deployment(editor)
    _, context = _request(editor)
    assert context["logging_status"]["state"] == "deployed"


def test_status_detects_shared_identity_override_and_unrelated_changes(logging_editor):
    editor = logging_editor
    _config(editor, "DD_SITE", "datadoghq.com", shared=True)
    _deployment(editor)
    _config(editor, "UNRELATED", "new")
    _, context = _request(editor)
    assert context["logging_status"]["state"] == "deployed"
    response, _ = _save(editor, site="datadoghq.com", override_site="y")
    assert response.status_code == 302
    _, context = _request(editor)
    assert context["logging_status"]["state"] == "pending"


@pytest.mark.parametrize("site", DATADOG_SITES)
def test_destination_test_uses_saved_tenant_key_not_draft(logging_editor, site):
    editor = logging_editor
    _config(editor, "DD_SITE", site, shared=True)
    _config(
        editor,
        "DD_API_KEY",
        "**secure**",
        shared=True,
        secret=True,
        key_slug="vault:tenant/key",
    )
    editor.read.side_effect = None
    editor.read.return_value = {"data": {"DD_API_KEY": "saved-test-key"}}
    editor.network.side_effect = None
    intake = Mock(status_code=202)
    editor.network.return_value = intake
    response, _ = _request(
        editor,
        data={
            "action": "test",
            "api_key": "unsaved-test-key",
            "site": "attacker.example",
        },
    )
    assert response.status_code == 302
    args, kwargs = editor.network.call_args
    assert args == (f"https://http-intake.logs.{site}/api/v2/logs",)
    assert kwargs["headers"] == {"DD-API-KEY": "saved-test-key"}
    assert kwargs["stream"] is True
    assert kwargs["timeout"] == (3.05, 5)
    assert kwargs["allow_redirects"] is False
    payload = json.dumps(kwargs["json"])
    assert "saved-test-key" not in payload
    assert "unsaved-test-key" not in payload
    assert editor.app.slug not in payload
    editor.read.assert_called_once_with("tenant/key", secret=True)
    editor.write.assert_not_called()
    intake.close.assert_called_once()
    with editor.client.session_transaction() as session:
        assert session["_flashes"][-1][0] == "success"
    assert _local(editor, "DD_LOGS_ENABLED") is None


@pytest.mark.parametrize("failure", [301, 403, 500, "timeout"])
def test_destination_failure_never_exposes_response_or_exception(
    logging_editor, failure
):
    editor = logging_editor
    _config(editor, "DD_SITE", "datadoghq.com")
    _config(editor, "DD_API_KEY", "saved-test-key")
    if failure == "timeout":
        editor.network.side_effect = requests.Timeout("sensitive-error-body")
    else:
        editor.network.side_effect = None
        editor.network.return_value = Mock(
            status_code=failure, text="sensitive-error-body"
        )
    response, _ = _request(editor, data={"action": "test"})
    assert response.status_code == 302
    with editor.client.session_transaction() as session:
        assert session["_flashes"][-1][0] == "error"
        assert "sensitive-error-body" not in str(session["_flashes"])
        assert "saved-test-key" not in str(session["_flashes"])
    editor.write.assert_not_called()


@pytest.mark.parametrize(
    "site,key",
    [
        ("https://attacker.example", "key"),
        ("datadoghq.com", "**secure**"),
        (None, None),
    ],
)
def test_invalid_saved_destination_never_sends_request(logging_editor, site, key):
    editor = logging_editor
    if site:
        _config(editor, "DD_SITE", site)
    if key:
        _config(editor, "DD_API_KEY", key)
    response, _ = _request(editor, data={"action": "test"})
    assert response.status_code == 302
    editor.network.assert_not_called()


def test_destination_secret_read_failure_is_safe(logging_editor):
    editor = logging_editor
    _config(editor, "DD_SITE", "datadoghq.com")
    _config(
        editor, "DD_API_KEY", "**secure**", secret=True, key_slug="vault:tenant/key"
    )
    editor.read.side_effect = RuntimeError("secret-store-sensitive-details")
    response, _ = _request(editor, data={"action": "test"})
    assert response.status_code == 302
    editor.network.assert_not_called()
    with editor.client.session_transaction() as session:
        assert "secret-store-sensitive-details" not in str(session["_flashes"])


def test_deleted_shared_values_are_not_effective(logging_editor):
    editor = logging_editor
    _config(editor, "DD_SITE", "datadoghq.eu", shared=True, deleted=True)
    assert logging_configuration(editor.app, editor.app_env)["site"] is None


def test_shared_updates_and_local_precedence_reflect_immediately(logging_editor):
    editor = logging_editor
    shared = _config(editor, "DD_SITE", "datadoghq.eu", shared=True)
    _, context = _request(editor)
    assert context["form"].site.data == "datadoghq.eu"
    assert context["logging_fields"]["site"]["source"] == "shared"
    shared.value = "us3.datadoghq.com"
    db.session.flush()
    _, context = _request(editor)
    assert context["form"].site.data == "us3.datadoghq.com"
    _config(editor, "DD_SITE", "datadoghq.com")
    _, context = _request(editor)
    assert context["form"].site.data == "datadoghq.com"
    assert context["logging_fields"]["site"]["source"] == "application"
    assert shared.value == "us3.datadoghq.com"


def test_key_rotation_changes_snapshot_version_without_exposing_value(logging_editor):
    editor = logging_editor
    _config(editor, "DD_API_KEY", "**secure**", secret=True, key_slug="vault:old")
    _deployment(editor)
    response, _ = _save(editor, api_key="replacement-test-key")
    assert response.status_code == 302
    response, context = _request(editor)
    assert context["logging_status"]["state"] == "pending"
    assert "replacement-test-key" not in response.get_data(as_text=True)
    assert _local(editor, "DD_API_KEY").value == "**secure**"
