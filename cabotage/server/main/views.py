from flask import render_template, Blueprint
from flask_login import current_user
from sqlalchemy import case, func
from sqlalchemy.orm import joinedload

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    Deployment,
    Image,
    Project,
)


main_blueprint = Blueprint(
    "main",
    __name__,
)


def _app_statuses(user_orgs):
    """Compute a status string per application_id for the dashboard.

    Returns dict[str, str] mapping app id (as str) to one of:
      "deploying", "deploy-error", "building", "build-error", "ok"

    Priority: deploying > deploy-error > building > build-error > ok
    Only considers the single most recent deploy/image per app.
    """
    # Latest deployment per app: is it in-progress or errored?
    latest_deploy = (
        db.session.query(
            Deployment.application_id,
            Deployment.complete,
            Deployment.error,
        )
        .join(Application)
        .join(Project)
        .filter(Project.organization_id.in_(user_orgs))
        .distinct(Deployment.application_id)
        .order_by(Deployment.application_id, Deployment.created.desc())
        .subquery()
    )

    deploy_status = db.session.query(
        latest_deploy.c.application_id,
        case(
            (
                (latest_deploy.c.complete == False) & (latest_deploy.c.error == False),  # noqa: E712
                "deploying",
            ),
            (latest_deploy.c.error == True, "deploy-error"),  # noqa: E712
            else_="ok",
        ).label("status"),
    ).all()

    statuses = {str(app_id): status for app_id, status in deploy_status}

    # Latest image per app: is it building or errored?
    latest_image = (
        db.session.query(
            Image.application_id,
            Image.built,
            Image.error,
        )
        .join(Application)
        .join(Project)
        .filter(Project.organization_id.in_(user_orgs))
        .distinct(Image.application_id)
        .order_by(Image.application_id, Image.created.desc())
        .subquery()
    )

    image_status = db.session.query(
        latest_image.c.application_id,
        case(
            (
                (latest_image.c.built == False) & (latest_image.c.error == False),  # noqa: E712
                "building",
            ),
            (latest_image.c.error == True, "build-error"),  # noqa: E712
            else_="ok",
        ).label("status"),
    ).all()

    for app_id, status in image_status:
        app_key = str(app_id)
        existing = statuses.get(app_key, "ok")
        # Deploy statuses take priority over image statuses
        if existing == "ok" and status != "ok":
            statuses[app_key] = status

    return statuses


@main_blueprint.route("/")
def home():
    project_count = 0
    app_count = 0
    deploy_count = 0
    user_organizations = []
    app_statuses = {}
    if current_user.is_authenticated:
        user_orgs = db.session.query(OrganizationMember.organization_id).filter(
            OrganizationMember.user_id == current_user.id
        )
        project_count = Project.query.filter(
            Project.organization_id.in_(user_orgs)
        ).count()
        app_count = (
            Application.query.join(Project)
            .filter(Project.organization_id.in_(user_orgs))
            .count()
        )
        deploy_count = (
            db.session.query(func.count(Deployment.id))
            .join(Application)
            .join(Project)
            .filter(
                Project.organization_id.in_(user_orgs),
                Deployment.complete == True,  # noqa: E712
            )
            .scalar()
        )
        user_organizations = (
            Organization.query.filter(Organization.id.in_(user_orgs))
            .options(
                joinedload(Organization.projects)
                .joinedload(Project.project_applications)
            )
            .order_by(Organization.name)
            .all()
        )
        if app_count:
            app_statuses = _app_statuses(user_orgs)
    return render_template(
        "main/home.html",
        project_count=project_count,
        app_count=app_count,
        deploy_count=deploy_count,
        user_organizations=user_organizations,
        app_statuses=app_statuses,
    )


@main_blueprint.route("/about/")
def about():
    return render_template("main/about.html")
