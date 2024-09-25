from flask import render_template, Blueprint
from flask_security import current_user


main_blueprint = Blueprint(
    "main",
    __name__,
)


@main_blueprint.route("/")
def home():
    if current_user.is_authenticated:
        projects = current_user.projects
        organizations = current_user.organizations
    else:
        projects, organizations = None, None
    return render_template("main/home.html", projects=projects, organizations=organizations)


@main_blueprint.route("/about/")
def about():
    return render_template("main/about.html")
