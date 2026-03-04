from flask import render_template, Blueprint
from flask_login import current_user
from sqlalchemy import func

from cabotage.server import db
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import Application, Deployment, Project


main_blueprint = Blueprint(
    "main",
    __name__,
)


@main_blueprint.route("/")
def home():
    project_count = 0
    app_count = 0
    deploy_count = 0
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
    return render_template(
        "main/home.html",
        project_count=project_count,
        app_count=app_count,
        deploy_count=deploy_count,
    )


@main_blueprint.route("/about/")
def about():
    return render_template("main/about.html")
