from flask import render_template, Blueprint
from flask_login import current_user


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
        for membership in current_user.organizations:
            for project in membership.organization.projects:
                project_count += 1
                for app in project.project_applications:
                    app_count += 1
                    deploy_count += app.deployments.filter_by(complete=True).count()
    return render_template(
        "main/home.html",
        project_count=project_count,
        app_count=app_count,
        deploy_count=deploy_count,
    )


@main_blueprint.route("/about/")
def about():
    return render_template("main/about.html")
