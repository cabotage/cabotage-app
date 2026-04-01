import datetime
import logging

from flask import Blueprint, abort, jsonify, request
from flask_security import current_user, login_required
from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import ARRAY, TEXT

from cabotage.server import db
from cabotage.server.acl import AdministerOrganizationPermission
from cabotage.server.models.auth import Organization
from cabotage.server.models.notifications import (
    NOTIFICATION_CATEGORIES,
    NotificationRoute,
)
from cabotage.server.models.projects import activity_plugin

Activity = activity_plugin.activity_cls

log = logging.getLogger(__name__)

notification_routing_bp = Blueprint(
    "notification_routing", __name__, url_prefix="/integrations/notifications"
)


def _get_org_or_403(org_slug):
    organization = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not AdministerOrganizationPermission(organization.id).can():
        abort(403)
    return organization


@notification_routing_bp.route("/<org_slug>/categories")
@login_required
def list_categories(org_slug):
    _get_org_or_403(org_slug)
    return jsonify({"categories": NOTIFICATION_CATEGORIES})


@notification_routing_bp.route("/<org_slug>/routes")
@login_required
def list_routes(org_slug):
    """Get all routes, optionally filtered by category.

    Query params: category (optional)
    """
    organization = _get_org_or_403(org_slug)

    query = NotificationRoute.query.filter_by(organization_id=organization.id)

    category = request.args.get("category")
    if category:
        if category not in NOTIFICATION_CATEGORIES:
            return jsonify({"error": "Invalid category"}), 400
        type_keys = [
            f"{category}.{t}" for t in NOTIFICATION_CATEGORIES[category]["types"]
        ]
        query = query.filter(
            NotificationRoute.notification_types.op("?|")(cast(type_keys, ARRAY(TEXT)))
        )

    routes = query.all()
    return jsonify(
        {
            "routes": [_route_to_dict(r) for r in routes],
        }
    )


@notification_routing_bp.route("/<org_slug>/routes", methods=["POST"])
@login_required
def save_route(org_slug):
    """Create or update a single route.

    JSON body: {
        notification_types: ["pipeline.deploy", "pipeline.release"],
        project_ids: ["uuid", ...],    // empty = any
        environment_ids: ["uuid", ...],
        application_ids: ["uuid", ...],
        integration: "slack",
        channel_id: "C001",
        channel_name: "#deploys",
        enabled: true
    }
    """
    organization = _get_org_or_403(org_slug)
    data = request.get_json()
    if not data:
        abort(400)

    ntypes = data.get("notification_types") or []
    if not ntypes:
        return jsonify({"error": "notification_types is required"}), 400

    # Validate all types
    all_valid = set()
    for cat_key, cat_info in NOTIFICATION_CATEGORIES.items():
        for t in cat_info["types"]:
            all_valid.add(f"{cat_key}.{t}")
    for ntype in ntypes:
        if ntype not in all_valid:
            return jsonify({"error": f"Invalid notification type: {ntype}"}), 400

    integration = data.get("integration")
    if not integration or integration not in ("slack", "discord"):
        return jsonify({"error": "integration must be 'slack' or 'discord'"}), 400

    channel_id = data.get("channel_id")
    if not channel_id:
        return jsonify({"error": "channel_id is required"}), 400

    project_ids = data.get("project_ids") or []
    environment_ids = data.get("environment_ids") or []
    application_ids = data.get("application_ids") or []

    route_id = data.get("id")
    if route_id:
        route = NotificationRoute.query.filter_by(
            id=route_id, organization_id=organization.id
        ).first_or_404()
        route.notification_types = ntypes
        route.project_ids = project_ids
        route.environment_ids = environment_ids
        route.application_ids = application_ids
        route.integration = integration
        route.channel_id = channel_id
        route.channel_name = data.get("channel_name")
        route.enabled = data.get("enabled", True)
    else:
        route = NotificationRoute(
            organization_id=organization.id,
            notification_types=ntypes,
            project_ids=project_ids,
            environment_ids=environment_ids,
            application_ids=application_ids,
            integration=integration,
            channel_id=channel_id,
            channel_name=data.get("channel_name"),
            enabled=data.get("enabled", True),
        )
        db.session.add(route)

    verb = "edit" if route_id else "create"
    db.session.flush()
    activity = Activity(
        verb=verb,
        object=organization,
        data={
            "user_id": str(current_user.id),
            "action": f"notification_route_{verb}",
            "notification_types": ntypes,
            "integration": integration,
            "channel_name": data.get("channel_name"),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
    )
    db.session.add(activity)
    db.session.commit()
    return jsonify(_route_to_dict(route)), 201


@notification_routing_bp.route("/<org_slug>/routes/<route_id>", methods=["DELETE"])
@login_required
def delete_route(org_slug, route_id):
    organization = _get_org_or_403(org_slug)
    route = NotificationRoute.query.filter_by(
        id=route_id, organization_id=organization.id
    ).first_or_404()
    ntypes = route.notification_types
    integration_name = route.integration
    db.session.delete(route)
    db.session.flush()
    activity = Activity(
        verb="delete",
        object=organization,
        data={
            "user_id": str(current_user.id),
            "action": "notification_route_delete",
            "notification_types": ntypes,
            "integration": integration_name,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
    )
    db.session.add(activity)
    db.session.commit()
    return jsonify({"status": "ok"})


@notification_routing_bp.route("/<org_slug>/scopes")
@login_required
def list_scopes(org_slug):
    organization = _get_org_or_403(org_slug)

    from cabotage.server.models.projects import Project

    projects = (
        Project.query.filter_by(organization_id=organization.id)
        .filter(Project.deleted_at.is_(None))
        .all()
    )

    result = []
    for project in projects:
        result.append(
            {
                "id": str(project.id),
                "name": project.name,
                "slug": project.slug,
                "environments": [
                    {"id": str(e.id), "name": e.name, "slug": e.slug}
                    for e in project.active_environments
                ],
                "applications": [
                    {"id": str(a.id), "name": a.name, "slug": a.slug}
                    for a in project.active_applications
                ],
            }
        )

    return jsonify({"projects": result})


def _route_to_dict(route):
    return {
        "id": str(route.id),
        "notification_types": route.notification_types or [],
        "project_ids": route.project_ids or [],
        "environment_ids": route.environment_ids or [],
        "application_ids": route.application_ids or [],
        "integration": route.integration,
        "channel_id": route.channel_id,
        "channel_name": route.channel_name,
        "enabled": route.enabled,
    }


def init_notification_routing(app):
    app.register_blueprint(notification_routing_bp)
