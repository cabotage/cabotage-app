import datetime
import uuid
from collections.abc import Iterator

import pytest
from flask import Flask

from cabotage.server import db
from cabotage.server.admin_console.instance import get_instance_application
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Ingress,
    IngressHost,
    Project,
)
from tests.test_organization_requests import app as app


@pytest.fixture
def instances(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[ApplicationEnvironment, ApplicationEnvironment]]:
    monkeypatch.setitem(app.config, "INSTANCE_APPLICATION_ENVIRONMENT_ID", "")
    marker = uuid.uuid4().hex[:12]
    rows = []
    for index in range(2):
        organization = Organization(
            name=f"Operator {index}", slug=f"instance-{marker}-{index}"
        )
        project = Project(
            name="Control Plane", slug="control-plane", organization=organization
        )
        application = Application(name="Navigator", slug="navigator", project=project)
        environment = Environment(
            name="Blue Region", slug="blue-region", project=project
        )
        app_env = ApplicationEnvironment(
            application=application, environment=environment
        )
        db.session.add(app_env)
        rows.append(app_env)
    db.session.flush()
    try:
        yield rows[0], rows[1]
    finally:
        db.session.rollback()


def _expected_link(app_env: ApplicationEnvironment) -> dict[str, str]:
    application = app_env.application
    project = application.project
    organization = project.organization
    environment = app_env.environment
    return {
        "name": application.name,
        "path": f"{organization.slug} / {project.slug} / {environment.slug}",
        "url": (
            f"/projects/{organization.slug}/{project.slug}"
            f"/env/{environment.slug}/applications/{application.slug}"
        ),
    }


def test_instance_link_selects_exact_tenant_and_environment(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    instances: tuple[ApplicationEnvironment, ApplicationEnvironment],
) -> None:
    selected, _ = instances
    other_environment = Environment(
        name="Green Region",
        slug="green-region",
        project=selected.application.project,
        is_default=True,
    )
    db.session.add(
        ApplicationEnvironment(
            application=selected.application, environment=other_environment
        )
    )
    db.session.flush()
    monkeypatch.setitem(
        app.config, "INSTANCE_APPLICATION_ENVIRONMENT_ID", str(selected.id)
    )
    with app.test_request_context("/admin/", base_url="https://untrusted.example"):
        assert get_instance_application() == _expected_link(selected)


def test_instance_link_follows_renamed_ancestry(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    instances: tuple[ApplicationEnvironment, ApplicationEnvironment],
) -> None:
    selected, _ = instances
    monkeypatch.setitem(
        app.config, "INSTANCE_APPLICATION_ENVIRONMENT_ID", str(selected.id)
    )
    with app.test_request_context("/admin/"):
        original = get_instance_application()
        for row in (
            selected.application,
            selected.application.project,
            selected.application.project.organization,
            selected.environment,
        ):
            row.name += " Renamed"
            row.slug += "-renamed"
        db.session.flush()
        assert get_instance_application() == _expected_link(selected)
        assert get_instance_application() != original


@pytest.mark.parametrize(
    "deleted_relationship",
    ["app_env", "application", "environment", "project", "organization"],
)
def test_instance_link_rejects_deleted_relationships(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    instances: tuple[ApplicationEnvironment, ApplicationEnvironment],
    deleted_relationship: str,
) -> None:
    selected, _ = instances
    monkeypatch.setitem(
        app.config, "INSTANCE_APPLICATION_ENVIRONMENT_ID", str(selected.id)
    )
    rows = {
        "app_env": selected,
        "application": selected.application,
        "environment": selected.environment,
        "project": selected.application.project,
        "organization": selected.application.project.organization,
    }
    rows[deleted_relationship].deleted_at = datetime.datetime.now(datetime.timezone.utc)
    db.session.flush()
    with app.test_request_context("/admin/"):
        assert get_instance_application() is None


def test_instance_link_rejects_cross_project_environment(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    instances: tuple[ApplicationEnvironment, ApplicationEnvironment],
) -> None:
    selected, other = instances
    monkeypatch.setitem(
        app.config, "INSTANCE_APPLICATION_ENVIRONMENT_ID", str(selected.id)
    )
    selected.environment = other.environment
    db.session.flush()
    with app.test_request_context("/admin/"):
        assert get_instance_application() is None


@pytest.mark.parametrize(
    "configured_id", [None, "", "not-a-uuid", 42, str(uuid.uuid4())]
)
def test_instance_link_never_guesses_from_host_or_duplicate_names(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    instances: tuple[ApplicationEnvironment, ApplicationEnvironment],
    configured_id: object,
) -> None:
    hostname = f"instance-{uuid.uuid4().hex}.example"
    for app_env in instances:
        db.session.add(
            Ingress(
                application_environment=app_env,
                hosts=[IngressHost(hostname=hostname, is_auto_generated=True)],
            )
        )
    db.session.flush()
    monkeypatch.setitem(app.config, "EXT_SERVER_NAME", hostname)
    monkeypatch.setitem(
        app.config, "INSTANCE_APPLICATION_ENVIRONMENT_ID", configured_id
    )
    with app.test_request_context("/admin/", base_url=f"https://{hostname}"):
        assert get_instance_application() is None


def test_instance_link_does_not_resolve_application_uuid(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    instances: tuple[ApplicationEnvironment, ApplicationEnvironment],
) -> None:
    selected, _ = instances
    monkeypatch.setitem(
        app.config, "INSTANCE_APPLICATION_ENVIRONMENT_ID", str(selected.application.id)
    )
    with app.test_request_context("/admin/"):
        assert get_instance_application() is None
