"""Request handling shared by the cost views."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from flask import (
    Response,
    abort,
    jsonify,
    request,
    url_for,
)
from flask_login import current_user
from flask_principal import Permission

from cabotage.server.acl import (
    ViewOrganizationPermission,
)
from cabotage.server.applets.cost.constants import (
    CUSTOM_RANGE_FLOOR,
    CUSTOM_RANGE_MAX_SPAN,
    DEFAULT_RANGE,
    PRIMARY_BREAKDOWN,
    RANGE_KEYS,
)
from cabotage.server.applets.cost.helpers import as_utc, str_or_none
from cabotage.server.applets.cost.reports import CostScope, build_report
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import Application, Environment, Project


def requested_range() -> str:
    """Parse ?range=, falling back to the default for anything unrecognized."""
    key = request.args.get("range", DEFAULT_RANGE)
    return key if key in RANGE_KEYS else DEFAULT_RANGE


def requested_window() -> tuple[datetime, datetime] | tuple[None, None]:
    """Parse ?start=&end= ISO timestamps for a brushed/custom range, if both present and valid."""
    start_raw = request.args.get("start")
    end_raw = request.args.get("end")
    if not start_raw or not end_raw:
        return None, None
    try:
        start = as_utc(datetime.fromisoformat(start_raw))
        end = as_utc(datetime.fromisoformat(end_raw))
    except ValueError, OverflowError:
        return None, None
    if not CUSTOM_RANGE_FLOOR <= start < end or start > datetime.now(UTC):
        return None, None
    if end - start > CUSTOM_RANGE_MAX_SPAN:
        return None, None
    return start, end


def report_for_request(scope: CostScope) -> dict[str, object]:
    """Build a report for the selected preset or custom time window."""
    range_key = requested_range()
    start, end = requested_window()
    if start is not None and end is not None:
        return build_report(scope, range_key, start=start, end=end)
    return build_report(scope, range_key)


def require_permission(permission: Permission) -> None:
    if not permission.can():
        abort(403)


def project_environments(project: Project) -> list[Environment]:
    """Active environments for a project, for the project-scope env toggle."""
    return sorted(
        (e for e in project.project_environments if e.deleted_at is None),
        key=lambda e: (e.sort_order, e.name),
    )


def application_environments(application: Application) -> list[Environment]:
    """Active environments this application is deployed to, for the
    application-scope env toggle (deduped, one per environment)."""
    seen: dict[uuid.UUID, Environment] = {}
    for ae in application.application_environments:
        if ae.deleted_at is not None:
            continue
        env = ae.environment
        if env is None or env.deleted_at is not None:
            continue
        seen[env.id] = env
    return sorted(seen.values(), key=lambda e: (e.sort_order, e.name))


def env_options_json(environments: Sequence[Environment]) -> list[dict[str, str]]:
    return [{"slug": e.slug, "name": e.name} for e in environments]


def requested_env(
    environments: Sequence[Environment], slug: str | None
) -> Environment | None:
    """None for no slug; 404 for a slug outside `environments`."""
    if not slug:
        return None
    for e in environments:
        if e.slug == slug:
            return e
    abort(404)


def row_url(
    organization: Organization | None,
    breakdown_key: str,
    row: dict[str, object],
    org_cache: dict[str, Organization | None],
) -> str | None:
    """Build the child scope's cost page URL for a breakdown row, if it has one.

    `organization` is the scope's own org (None for platform scope, where each row can
    belong to a different org — resolved per-row from ``row["slugs"]["organization"]``,
    memoized in `org_cache` since the same handful of orgs repeat across top-7 rows).
    """
    if row.get("deleted"):
        return None
    slugs_raw = row.get("slugs")
    slugs: dict[str, object] = slugs_raw if isinstance(slugs_raw, dict) else {}
    org_slug = str_or_none(slugs.get("organization"))
    org = organization
    if org_slug and (organization is None or organization.slug != org_slug):
        if org_slug not in org_cache:
            candidate: Organization | None = (
                Organization.query.filter_by(slug=org_slug)
                .filter(Organization.deleted_at.is_(None))
                .first()
            )
            org_cache[org_slug] = candidate
        org = org_cache[org_slug]
    if org is None:
        return None
    if organization is None and not ViewOrganizationPermission(org.id).can():
        return None
    project_slug = str_or_none(slugs.get("project"))
    environment_slug = str_or_none(slugs.get("environment"))
    application_slug = str_or_none(slugs.get("application"))

    if breakdown_key == "organizations" and org_slug:
        return url_for("cost.organization_cost", org_slug=org.slug)
    if breakdown_key == "projects" and project_slug:
        return url_for(
            "cost.project_cost", org_slug=org.slug, project_slug=project_slug
        )
    if breakdown_key == "environments" and project_slug and environment_slug:
        return url_for(
            "cost.environment_cost",
            org_slug=org.slug,
            project_slug=project_slug,
            env_slug=environment_slug,
        )
    if breakdown_key == "applications" and project_slug and application_slug:
        return url_for(
            "cost.application_cost",
            org_slug=org.slug,
            project_slug=project_slug,
            app_slug=application_slug,
        )
    return None


def report_json(organization: Organization | None, scope: CostScope) -> Response:
    report = report_for_request(scope)
    org_cache: dict[str, Organization | None] = {}
    breakdowns = cast(
        dict[str, list[dict[str, object]]], report.get("breakdowns") or {}
    )
    for key, rows in breakdowns.items():
        for row in rows:
            row["url"] = row_url(organization, key, row, org_cache)
    primary_rows = breakdowns.get(PRIMARY_BREAKDOWN.get(scope.kind, ""), [])
    url_by_key = {row.get("key"): row.get("url") for row in primary_rows}
    for row in cast(list[dict[str, object]], report.get("movers") or []):
        row["url"] = url_by_key.get(row.get("key"))
    return jsonify(report)


def require_cost_admin() -> None:
    """Mirrors user.views._require_admin: abort 403 unless the current user is a super admin."""
    if not current_user.admin:
        abort(403)
