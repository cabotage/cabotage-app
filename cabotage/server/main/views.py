from flask import render_template, Blueprint


main_blueprint = Blueprint(
    "main",
    __name__,
)


@main_blueprint.route("/")
def home():
    return render_template("main/home.html")


@main_blueprint.route("/about/")
def about():
    return render_template("main/about.html")


@main_blueprint.route("/user-interest")
def interest():
    return render_template("main/interest.html")
