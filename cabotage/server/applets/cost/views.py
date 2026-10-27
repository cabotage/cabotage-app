"""Read-only cost dashboard routes.

Pages mirror the ``/observe`` URL shapes with ``/cost`` and each has a JSON
twin at ``<page>/data`` returning :func:`cabotage.server.applets.cost.reports.build_report`
plus a server-built ``url`` on every breakdown row (so the client never has to
know route shapes). No url_prefix; every route 404s unless
``SHOW_COST_ESTIMATES`` is enabled.
"""

from typing import cast

from celery import Task
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required
from kombu.exceptions import OperationalError
from werkzeug.wrappers import Response as RedirectResponse

from cabotage.celery.tasks.cost import collect_costs
from cabotage.server.acl import (
    ViewApplicationPermission,
    ViewOrganizationPermission,
    ViewProjectPermission,
)
from cabotage.server.applets.cost.reports import CostScope
from cabotage.server.applets.cost.services import (
    application_environments,
    env_options_json,
    project_environments,
    report_json,
    requested_env,
    require_cost_admin,
    require_permission,
)
from cabotage.server.query_helpers import (
    application_or_404,
    environment_or_404,
    organization_or_404,
    project_or_404,
)

cost_blueprint = Blueprint("cost", __name__)


@cost_blueprint.before_request
def _require_cost_enabled() -> None:
    if not current_app.config.get("SHOW_COST_ESTIMATES"):
        abort(404)


# ── Organization ──


@cost_blueprint.route("/organizations/<org_slug>/cost")
@login_required
def organization_cost(org_slug: str) -> str:
    organization = organization_or_404(org_slug)
    require_permission(ViewOrganizationPermission(organization.id))
    return render_template("cost/organization.html", organization=organization)


@cost_blueprint.route("/organizations/<org_slug>/cost/data")
@login_required
def organization_cost_data(org_slug: str) -> Response:
    organization = organization_or_404(org_slug)
    require_permission(ViewOrganizationPermission(organization.id))
    scope = CostScope(kind="organization", organization=organization)
    return report_json(organization, scope)


# ── Project ──


@cost_blueprint.route("/projects/<org_slug>/<project_slug>/cost")
@login_required
def project_cost(org_slug: str, project_slug: str) -> str:
    organization = organization_or_404(org_slug)
    project = project_or_404(organization, project_slug)
    require_permission(ViewProjectPermission(project.id))
    environments = env_options_json(project_environments(project))
    return render_template(
        "cost/project.html",
        organization=organization,
        project=project,
        environments=environments,
    )


@cost_blueprint.route("/projects/<org_slug>/<project_slug>/cost/data")
@login_required
def project_cost_data(org_slug: str, project_slug: str) -> Response:
    organization = organization_or_404(org_slug)
    project = project_or_404(organization, project_slug)
    require_permission(ViewProjectPermission(project.id))
    environment = requested_env(project_environments(project), request.args.get("env"))
    if environment is not None:
        scope = CostScope(
            kind="environment",
            organization=organization,
            project=project,
            environment=environment,
        )
    else:
        scope = CostScope(kind="project", organization=organization, project=project)
    return report_json(organization, scope)


# ── Environment ──


@cost_blueprint.route(
    "/projects/<org_slug>/<project_slug>/environments/<env_slug>/cost"
)
@login_required
def environment_cost(org_slug: str, project_slug: str, env_slug: str) -> str:
    organization = organization_or_404(org_slug)
    project = project_or_404(organization, project_slug)
    require_permission(ViewProjectPermission(project.id))
    environment = environment_or_404(project, env_slug)
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
    organization = organization_or_404(org_slug)
    project = project_or_404(organization, project_slug)
    require_permission(ViewProjectPermission(project.id))
    environment = environment_or_404(project, env_slug)
    scope = CostScope(
        kind="environment",
        organization=organization,
        project=project,
        environment=environment,
    )
    return report_json(organization, scope)


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
    organization = organization_or_404(org_slug)
    project = project_or_404(organization, project_slug)
    application = application_or_404(project, app_slug)
    require_permission(ViewApplicationPermission(application.id))
    app_environments = application_environments(application)
    environment = requested_env(app_environments, env_slug)
    environments = env_options_json(app_environments)
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
    organization = organization_or_404(org_slug)
    project = project_or_404(organization, project_slug)
    application = application_or_404(project, app_slug)
    require_permission(ViewApplicationPermission(application.id))
    environment = requested_env(
        application_environments(application), request.args.get("env")
    )
    scope = CostScope(
        kind="application",
        organization=organization,
        project=project,
        application=application,
        environment=environment,
    )
    return report_json(organization, scope)


# ── Platform (admin-only, install-wide) ──


@cost_blueprint.route("/infra/cost")
@login_required
def platform_cost() -> str:
    require_cost_admin()
    return render_template("cost/platform.html")


@cost_blueprint.route("/infra/cost/data")
@login_required
def platform_cost_data() -> Response:
    require_cost_admin()
    scope = CostScope(kind="platform", organization=None)
    return report_json(None, scope)


@cost_blueprint.route("/infra/cost/collect", methods=["POST"])
@login_required
def platform_cost_collect() -> RedirectResponse:
    """The admin "Collect now" button on /infra/cost: queue `collect_costs`."""
    require_cost_admin()
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
