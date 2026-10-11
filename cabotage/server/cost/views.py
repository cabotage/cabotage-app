"""Read-only cost dashboard routes.

Pages mirror the ``/observe`` URL shapes with ``/cost`` and each has a JSON
twin at ``<page>/data`` returning :func:`cabotage.utils.cost.reports.build_report`
plus a server-built ``url`` on every breakdown row (so the client never has to
know route shapes). No url_prefix; every route 404s unless
``SHOW_COST_ESTIMATES`` is enabled.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import cast

from celery import Task
from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from kombu.exceptions import OperationalError
from werkzeug.wrappers import Response as RedirectResponse

from cabotage.server.acl import (
    ViewApplicationPermission,
    ViewOrganizationPermission,
    ViewProjectPermission,
)
from cabotage.celery.tasks.cost import collect_costs
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import Application, Environment, Project
from cabotage.utils.cost.reports import CostScope, build_report

cost_blueprint = Blueprint("cost", __name__)


@cost_blueprint.before_request
def _require_cost_enabled() -> None:
    if not current_app.config.get("SHOW_COST_ESTIMATES"):
        abort(404)


_RANGE_KEYS = {"24h", "7d", "30d", "mtd", "90d"}
_DEFAULT_RANGE = "mtd"

_PRIMARY_BREAKDOWN = {
    "organization": "projects",
    "project": "applications",
    "environment": "applications",
    "application": "processes",
    "platform": "organizations",
}


def _range_key() -> str:
    """Parse ?range=, falling back to the default for anything unrecognized."""
    key = request.args.get("range", _DEFAULT_RANGE)
    return key if key in _RANGE_KEYS else _DEFAULT_RANGE


def _custom_window() -> tuple[datetime, datetime] | tuple[None, None]:
    """Parse ?start=&end= ISO timestamps for a brushed/custom range, if both present and valid."""
    start_raw = request.args.get("start")
    end_raw = request.args.get("end")
    if not start_raw or not end_raw:
        return None, None
    try:
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    return start, end


def _build_report(scope: CostScope) -> dict[str, object]:
    """Build a report for the selected preset or custom time window."""
    range_key = _range_key()
    start, end = _custom_window()
    if start is not None and end is not None:
        return build_report(scope, range_key, start=start, end=end)
    return build_report(scope, range_key)


def _lookup_org(org_slug: str) -> Organization:
    organization: Organization = (
        Organization.query.filter_by(slug=org_slug)
        .filter(Organization.deleted_at.is_(None))
        .first_or_404()
    )
    if not ViewOrganizationPermission(organization.id).can():
        abort(403)
    return organization


def _lookup_project(org_slug: str, project_slug: str) -> tuple[Organization, Project]:
    organization: Organization = (
        Organization.query.filter_by(slug=org_slug)
        .filter(Organization.deleted_at.is_(None))
        .first_or_404()
    )
    project: Project = (
        Project.query.filter_by(organization_id=organization.id, slug=project_slug)
        .filter(Project.deleted_at.is_(None))
        .first_or_404()
    )
    if not ViewProjectPermission(project.id).can():
        abort(403)
    return organization, project


def _project_environments(project: Project) -> list[Environment]:
    """Active environments for a project, for the project-scope env toggle."""
    return sorted(
        (e for e in project.project_environments if e.deleted_at is None),
        key=lambda e: (e.sort_order, e.name),
    )


def _application_environments(application: Application) -> list[Environment]:
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


def _env_options_json(environments: Sequence[Environment]) -> list[dict[str, str]]:
    return [{"slug": e.slug, "name": e.name} for e in environments]


def _match_env(
    environments: Sequence[Environment], slug: str | None
) -> Environment | None:
    if not slug:
        return None
    for e in environments:
        if e.slug == slug:
            return e
    return None


def _lookup_environment(
    org_slug: str, project_slug: str, env_slug: str
) -> tuple[Organization, Project, Environment]:
    organization, project = _lookup_project(org_slug, project_slug)
    environment: Environment = (
        Environment.query.filter_by(project_id=project.id, slug=env_slug)
        .filter(Environment.deleted_at.is_(None))
        .first_or_404()
    )
    return organization, project, environment


def _lookup_application(
    org_slug: str, project_slug: str, app_slug: str
) -> tuple[Organization, Project, Application]:
    organization, project = _lookup_project(org_slug, project_slug)
    application: Application = (
        Application.query.filter_by(project_id=project.id, slug=app_slug)
        .filter(Application.deleted_at.is_(None))
        .first_or_404()
    )
    if not ViewApplicationPermission(application.id).can():
        abort(403)
    return organization, project, application


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _row_url(
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
    slugs_raw = row.get("slugs")
    slugs: dict[str, object] = slugs_raw if isinstance(slugs_raw, dict) else {}
    org_slug = _str_or_none(slugs.get("organization"))
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
    project_slug = _str_or_none(slugs.get("project"))
    environment_slug = _str_or_none(slugs.get("environment"))
    application_slug = _str_or_none(slugs.get("application"))

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


def _report_json(organization: Organization | None, scope: CostScope) -> Response:
    report = _build_report(scope)
    org_cache: dict[str, Organization | None] = {}
    breakdowns = report.get("breakdowns")
    if isinstance(breakdowns, dict):
        for key, rows in breakdowns.items():
            if not isinstance(key, str) or not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    row["url"] = _row_url(organization, key, row, org_cache)
    primary_key = _PRIMARY_BREAKDOWN.get(scope.kind)
    movers = report.get("movers")
    if isinstance(movers, list):
        for row in movers:
            if isinstance(row, dict):
                row["url"] = (
                    _row_url(organization, primary_key, row, org_cache)
                    if primary_key
                    else None
                )
    return jsonify(report)


# ── Organization ──


@cost_blueprint.route("/organizations/<org_slug>/cost")
@login_required
def organization_cost(org_slug: str) -> str:
    organization = _lookup_org(org_slug)
    return render_template("cost/organization.html", organization=organization)


@cost_blueprint.route("/organizations/<org_slug>/cost/data")
@login_required
def organization_cost_data(org_slug: str) -> Response:
    organization = _lookup_org(org_slug)
    scope = CostScope(kind="organization", organization=organization)
    return _report_json(organization, scope)


# ── Project ──


@cost_blueprint.route("/projects/<org_slug>/<project_slug>/cost")
@login_required
def project_cost(org_slug: str, project_slug: str) -> str:
    organization, project = _lookup_project(org_slug, project_slug)
    environments = _env_options_json(_project_environments(project))
    return render_template(
        "cost/project.html",
        organization=organization,
        project=project,
        environments=environments,
    )


@cost_blueprint.route("/projects/<org_slug>/<project_slug>/cost/data")
@login_required
def project_cost_data(org_slug: str, project_slug: str) -> Response:
    organization, project = _lookup_project(org_slug, project_slug)
    environment = _match_env(_project_environments(project), request.args.get("env"))
    if environment is not None:
        scope = CostScope(
            kind="environment",
            organization=organization,
            project=project,
            environment=environment,
        )
    else:
        scope = CostScope(kind="project", organization=organization, project=project)
    return _report_json(organization, scope)


# ── Environment ──


@cost_blueprint.route(
    "/projects/<org_slug>/<project_slug>/environments/<env_slug>/cost"
)
@login_required
def environment_cost(org_slug: str, project_slug: str, env_slug: str) -> str:
    organization, project, environment = _lookup_environment(
        org_slug, project_slug, env_slug
    )
    return render_template(
        "cost/environment.html",
        organization=organization,
        project=project,
        environment=environment,
    )


@cost_blueprint.route(
    "/projects/<org_slug>/<project_slug>/environments/<env_slug>/cost/data"
)
@login_required
def environment_cost_data(org_slug: str, project_slug: str, env_slug: str) -> Response:
    organization, project, environment = _lookup_environment(
        org_slug, project_slug, env_slug
    )
    scope = CostScope(
        kind="environment",
        organization=organization,
        project=project,
        environment=environment,
    )
    return _report_json(organization, scope)


# ── Application ──


@cost_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/cost",
)
@cost_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/cost",
    defaults={"env_slug": None},
)
@login_required
def application_cost(
    org_slug: str, project_slug: str, app_slug: str, env_slug: str | None = None
) -> str:
    organization, project, application = _lookup_application(
        org_slug, project_slug, app_slug
    )
    environment: Environment | None = None
    if env_slug:
        environment = (
            Environment.query.filter_by(project_id=project.id, slug=env_slug)
            .filter(Environment.deleted_at.is_(None))
            .first_or_404()
        )
    environments = _env_options_json(_application_environments(application))
    return render_template(
        "cost/application.html",
        organization=organization,
        project=project,
        application=application,
        environment=environment,
        environments=environments,
    )


@cost_blueprint.route(
    "/projects/<org_slug>/<project_slug>/env/<env_slug>/applications/<app_slug>/cost/data",
)
@cost_blueprint.route(
    "/projects/<org_slug>/<project_slug>/applications/<app_slug>/cost/data",
    defaults={"env_slug": None},
)
@login_required
def application_cost_data(
    org_slug: str, project_slug: str, app_slug: str, env_slug: str | None = None
) -> Response:
    organization, project, application = _lookup_application(
        org_slug, project_slug, app_slug
    )
    environments = _application_environments(application)
    environment = _match_env(environments, request.args.get("env") or env_slug)
    scope = CostScope(
        kind="application",
        organization=organization,
        project=project,
        application=application,
        environment=environment,
    )
    return _report_json(organization, scope)


# ── Platform (admin-only, install-wide) ──


def _require_cost_admin() -> None:
    """Mirrors user.views._require_admin: abort 403 unless the current user is a super admin."""
    if not current_user.admin:
        abort(403)


@cost_blueprint.route("/infra/cost")
@login_required
def platform_cost() -> str:
    _require_cost_admin()
    return render_template("cost/platform.html")


@cost_blueprint.route("/infra/cost/data")
@login_required
def platform_cost_data() -> Response:
    _require_cost_admin()
    scope = CostScope(kind="platform", organization=None)
    return _report_json(None, scope)


@cost_blueprint.route("/infra/cost/collect", methods=["POST"])
@login_required
def platform_cost_collect() -> RedirectResponse:
    _require_cost_admin()
    try:
        _ = cast(Task, collect_costs).delay()
    except OperationalError, OSError:
        current_app.logger.exception("Could not queue cost collection")
        flash(
            "Could not queue collection. Try again when the worker broker is available.",
            "error",
        )
    else:
        flash(
            "Collection queued. The worker will collect the latest hour and backfill "
            "missing hours. Refresh this page after it finishes.",
            "success",
        )
    return redirect(
        url_for(
            "cost.platform_cost",
            range=request.args.get("range"),
            start=request.args.get("start"),
            end=request.args.get("end"),
        ),
        code=303,
    )
