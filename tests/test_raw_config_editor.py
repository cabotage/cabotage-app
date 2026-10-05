"""Tests for the application config Raw Editor bulk submission."""

import re
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
    Configuration,
    Environment,
    Project,
)
from cabotage.server.user import views
from cabotage.server.wsgi import app as _app

KEY_SLUGS = {"config_key_slug": "cfg/slug", "build_key_slug": None}


@pytest.fixture
def raw_editor(monkeypatch):
    for key, value in {
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "REQUIRE_MFA": False,
    }.items():
        monkeypatch.setitem(_app.config, key, value)
    with _app.app_context(), patch.object(db.session, "commit", db.session.flush):
        try:
            yield _raw_editor()
        finally:
            db.session.rollback()


def _raw_editor():
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
    db.session.add(
        ApplicationEnvironment(
            application_id=application.id,
            environment_id=environment.id,
            k8s_identifier=None,
        )
    )
    db.session.flush()
    client = _app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = user.fs_uniquifier
        sess["_fresh"] = True
        sess["fs_cc"] = "set"
        sess["fs_paa"] = time.time()
        sess["identity.id"] = user.id
        sess["identity.auth_type"] = "session"

    url = (
        f"/projects/{application.project.organization.slug}/"
        f"{application.project.slug}/applications/{application.slug}/config"
    )

    page = client.get(url)
    assert page.status_code == 200
    form = re.search(
        r'<div id="raw-editor-modal".*?<form action="([^"]*)"(.*?)</form>',
        page.get_data(as_text=True),
        re.S,
    )
    assert form, "Raw Editor form not rendered"
    fields = dict(
        re.findall(r'type="hidden" name="(\w+)" [^>]*value="([^"]*)"', form[2])
    )
    return client, application, form[1], fields


def _configs(application):
    return {
        c.name: c
        for c in Configuration.query.filter_by(application_id=application.id).all()
    }


def test_env_paste_creates_variables(raw_editor):
    client, application, action, fields = raw_editor
    with patch.object(
        views.config_writer, "write_configuration", return_value=KEY_SLUGS
    ):
        response = client.post(
            action,
            data={
                **fields,
                "raw_text": '# comment\n\nJUNIOR_SLASH_COMMAND=/pyper\nQUOTED="keep=quotes" ',
            },
        )

    assert response.status_code == 302
    assert {name: config.value for name, config in _configs(application).items()} == {
        "JUNIOR_SLASH_COMMAND": "/pyper",
        "QUOTED": '"keep=quotes" ',
    }


def test_json_update_preserves_secure_placeholder(raw_editor):
    client, application, action, fields = raw_editor
    for name, value, secret in [("PLAIN", "old", False), ("TOKEN", "**secure**", True)]:
        db.session.add(
            Configuration(
                application_id=application.id,
                application_environment_id=application.default_app_env.id,
                name=name,
                value=value,
                secret=secret,
            )
        )
    db.session.flush()
    with patch.object(
        views.config_writer, "write_configuration", return_value=KEY_SLUGS
    ) as write:
        response = client.post(
            action,
            data={
                **fields,
                "format": "json",
                "raw_text": '{"PLAIN": "new", "TOKEN": "**secure**"}',
            },
        )

    assert response.status_code == 302
    write.assert_called_once()
    configs = _configs(application)
    assert configs["PLAIN"].value == "new"
    assert (configs["TOKEN"].value, configs["TOKEN"].secret) == ("**secure**", True)


@pytest.mark.parametrize(
    "raw", ["GOOD=1\nnot a pair", "GOOD=1\n1BAD=x", "GOOD=1\nCABOTAGE_SENTINEL=x"]
)
def test_invalid_batch_writes_nothing(raw_editor, raw):
    client, application, action, fields = raw_editor
    with patch.object(
        views.config_writer, "write_configuration", return_value=KEY_SLUGS
    ) as write:
        response = client.post(action, data={**fields, "raw_text": raw})

    assert response.status_code == 302
    write.assert_not_called()
    assert _configs(application) == {}


@pytest.mark.parametrize(
    ("fmt", "raw"),
    [
        ("env", "DUP=1\nDUP=2"),
        ("json", '{"DUP": "1", "DUP": "2"}'),
    ],
)
def test_duplicate_names_write_nothing(raw_editor, fmt, raw):
    client, application, action, fields = raw_editor
    with patch.object(
        views.config_writer, "write_configuration", return_value=KEY_SLUGS
    ) as write:
        response = client.post(action, data={**fields, "format": fmt, "raw_text": raw})

    assert response.status_code == 302
    write.assert_not_called()
    assert _configs(application) == {}


def test_value_too_long_writes_nothing(raw_editor):
    client, application, action, fields = raw_editor
    with patch.object(
        views.config_writer, "write_configuration", return_value=KEY_SLUGS
    ) as write:
        response = client.post(
            action, data={**fields, "raw_text": f"TOO_LONG={'x' * 2049}"}
        )

    assert response.status_code == 302
    write.assert_not_called()
    assert _configs(application) == {}


def test_existing_name_match_is_case_insensitive(raw_editor):
    client, application, action, fields = raw_editor
    db.session.add(
        Configuration(
            application_id=application.id,
            application_environment_id=application.default_app_env.id,
            name="FOO",
            value="old",
        )
    )
    db.session.flush()
    with patch.object(
        views.config_writer, "write_configuration", return_value=KEY_SLUGS
    ) as write:
        response = client.post(action, data={**fields, "raw_text": "foo=new"})

    assert response.status_code == 302
    write.assert_called_once()
    configs = _configs(application)
    assert list(configs) == ["FOO"]
    assert configs["FOO"].value == "new"


def test_write_failure_rolls_back_batch(raw_editor):
    client, application, action, fields = raw_editor
    with patch.object(
        views.config_writer,
        "write_configuration",
        side_effect=[KEY_SLUGS, RuntimeError("config store unavailable")],
    ) as write:
        response = client.post(action, data={**fields, "raw_text": "ONE=1\nTWO=2"})

    assert response.status_code == 302
    assert write.call_count == 2
    assert _configs(application) == {}
