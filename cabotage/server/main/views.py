from flask import render_template, Blueprint
from flask_login import current_user
from sqlalchemy import func

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import Application, Deployment, Project

main_blueprint = Blueprint(
    "main",
    __name__,
)


@main_blueprint.route("/")
def home():
    org_count = 0
    project_count = 0
    app_count = 0
    deploy_count = 0
    if current_user.is_authenticated:
        user_orgs = (
            db.session.query(OrganizationMember.organization_id)
            .join(Organization, OrganizationMember.organization_id == Organization.id)
            .filter(
                OrganizationMember.user_id == current_user.id,
                Organization.deleted_at.is_(None),
            )
        )
        org_count = user_orgs.count()
        project_count = Project.query.filter(
            Project.organization_id.in_(user_orgs),
            Project.deleted_at.is_(None),
        ).count()
        app_count = (
            Application.query.join(Project)
            .filter(
                Project.organization_id.in_(user_orgs),
                Project.deleted_at.is_(None),
                Application.deleted_at.is_(None),
            )
            .count()
        )
        deploy_count = (
            db.session.query(func.count(Deployment.id))
            .join(Application)
            .join(Project)
            .filter(
                Project.organization_id.in_(user_orgs),
                Project.deleted_at.is_(None),
                Application.deleted_at.is_(None),
                Deployment.complete == True,  # noqa: E712
            )
            .scalar()
        )
    return render_template(
        "main/home.html",
        org_count=org_count,
        project_count=project_count,
        app_count=app_count,
        deploy_count=deploy_count,
    )


@main_blueprint.route("/about/")
def about():
    return render_template("main/about.html")
