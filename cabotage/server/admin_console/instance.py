"""Resolve this console's hosting application without inferring tenant identity."""

from uuid import UUID

from flask import current_app, url_for
from sqlalchemy import select

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Project,
)


def get_instance_application() -> dict[str, str] | None:
    """Return the configured, live application environment's native overview link.

    Hostnames, source commits, and runtime tags are not unique application-
    environment identities. An explicit UUID also survives application renames
    without accidentally selecting another tenant that reuses the old name.
    """
    configured_id = current_app.config.get("INSTANCE_APPLICATION_ENVIRONMENT_ID")
    if not isinstance(configured_id, str) or not configured_id.strip():
        return None
    try:
        app_env_id = UUID(configured_id.strip())
    except ValueError:
        return None

    row = db.session.execute(
        select(
            Application.name,
            Organization.slug,
            Project.slug,
            Environment.slug,
            Application.slug,
        )
        .select_from(ApplicationEnvironment)
        .join(Application, ApplicationEnvironment.application_id == Application.id)
        .join(Project, Application.project_id == Project.id)
        .join(Organization, Project.organization_id == Organization.id)
        .join(Environment, ApplicationEnvironment.environment_id == Environment.id)
        .where(
            ApplicationEnvironment.id == app_env_id,
            ApplicationEnvironment.deleted_at.is_(None),
            Application.deleted_at.is_(None),
            Project.deleted_at.is_(None),
            Organization.deleted_at.is_(None),
            Environment.deleted_at.is_(None),
            Environment.project_id == Project.id,
        )
    ).one_or_none()
    if row is None:
        return None

    name, org_slug, project_slug, env_slug, app_slug = row
    return {
        "name": name,
        "path": f"{org_slug} / {project_slug} / {env_slug}",
        "url": url_for(
            "user.project_application",
            org_slug=org_slug,
            project_slug=project_slug,
            env_slug=env_slug,
            app_slug=app_slug,
            _external=False,
        ),
    }
