"""Read-only cost-viewing routes.

Mounted under ``/cost`` when ``SHOW_COST_ESTIMATES`` is enabled. No payments,
no subscriptions, no external API calls — everything is derived locally from
``ApplicationEnvironment`` resource requests and the static AWS rate table.
"""

import logging

from flask import Blueprint, Response, jsonify, render_template
from flask_login import login_required

from cabotage.server.models import Organization
from cabotage.server.models.projects import Environment, Project
from cabotage.utils.cost.metering import (
    collect_environment_usage,
    collect_org_usage,
    collect_project_usage,
    get_meter_cost_breakdown,
    get_service_cost_list,
)

logger = logging.getLogger(__name__)

cost_blueprint = Blueprint(
    "cost",
    __name__,
    url_prefix="/cost",
)


@cost_blueprint.route("/")
@login_required
def cost_index() -> str:
    """Picker page — lists all orgs the user belongs to."""
    return render_template("cost/index.html")


@cost_blueprint.route("/<org_slug>/", methods=["GET"])
@login_required
def org_cost(org_slug: str) -> str:
    """Org-level cost overview — instant render, JSON tabs load on demand."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    return render_template("cost/org.html", org=org)


@cost_blueprint.route("/<org_slug>/org-cost")
@login_required
def org_cost_data(org_slug: str) -> Response:
    """JSON endpoint — org-wide totals, per-meter breakdown, per-service list."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    try:
        org_data = collect_org_usage(org)
        services = get_service_cost_list(org)
        meters = get_meter_cost_breakdown(org_data["totals"])
        return jsonify(
            total_cost=org_data["total_cost"],
            services=services,
            meters=meters,
            project_count=len(org_data["projects"]),
        )
    except Exception:
        logger.exception("Failed to build org cost for %s", org_slug)
        return jsonify(total_cost=0, services=[], meters=[], project_count=0)


@cost_blueprint.route("/<org_slug>/project-cost/<project_slug>")
@login_required
def project_cost_data(org_slug: str, project_slug: str) -> Response:
    """Per-service usage and cost for a single project."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=org.id, slug=project_slug,
    ).first_or_404()

    try:
        return jsonify(**collect_project_usage(project))
    except Exception:
        logger.exception("Failed to build project cost for %s/%s", org_slug, project_slug)
        return jsonify(
            project=project.name, services=[], totals={}, total_cost=0,
        )


@cost_blueprint.route("/<org_slug>/env-cost/<project_slug>/<env_slug>")
@login_required
def env_cost_data(org_slug: str, project_slug: str, env_slug: str) -> Response:
    """Per-service usage and cost for a single environment."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=org.id, slug=project_slug,
    ).first_or_404()
    environment = Environment.query.filter_by(
        project_id=project.id, slug=env_slug,
    ).first_or_404()

    try:
        return jsonify(**collect_environment_usage(environment))
    except Exception:
        logger.exception(
            "Failed to build env cost for %s/%s/%s", org_slug, project_slug, env_slug,
        )
        return jsonify(
            environment=environment.name, services=[], totals={}, total_cost=0,
        )
