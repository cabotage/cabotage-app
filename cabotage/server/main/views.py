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
        user_apps = Application.query.join(Project).filter(
            Project.organization_id.in_(user_orgs)
        )
        project_count = Project.query.filter(
            Project.organization_id.in_(user_orgs)
        ).count()
        app_count = user_apps.count()
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
        # Per-app status: find apps with in-flight deploys, builds, or errors
        user_app_ids = user_apps.with_entities(Application.id)
        for app_id, in (
            db.session.query(Deployment.application_id)
            .filter(
                Deployment.application_id.in_(user_app_ids),
                Deployment.complete == False,  # noqa: E712
                Deployment.error == False,  # noqa: E712
            )
            .distinct()
        ):
            app_statuses[str(app_id)] = "deploying"
        for app_id, in (
            db.session.query(Deployment.application_id)
            .filter(
                Deployment.application_id.in_(user_app_ids),
                Deployment.error == True,  # noqa: E712
            )
            .distinct()
        ):
            app_statuses.setdefault(str(app_id), "deploy-error")
        for app_id, in (
            db.session.query(Image.application_id)
            .filter(
                Image.application_id.in_(user_app_ids),
                Image.built == False,  # noqa: E712
                Image.error == False,  # noqa: E712
            )
            .distinct()
        ):
            app_statuses.setdefault(str(app_id), "building")
        for app_id, in (
            db.session.query(Image.application_id)
            .filter(
                Image.application_id.in_(user_app_ids),
                Image.error == True,  # noqa: E712
            )
            .distinct()
        ):
            app_statuses.setdefault(str(app_id), "build-error")
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
