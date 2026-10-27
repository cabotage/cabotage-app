from typing import cast

from flask import Blueprint, current_app, render_template, request
from flask_login import current_user

from cabotage.server.main.dashboard import dashboard_context
from cabotage.server.models.auth import User

main_blueprint = Blueprint(
    "main",
    __name__,
)


@main_blueprint.route("/")
def home() -> str:
    organization_requests_enabled = bool(
        current_app.config.get("ORGANIZATION_REQUESTS_ENABLED", False)
    )
    user = cast(User, current_user)
    if not user.is_authenticated:
        return render_template(
            "main/home.html",
            organization_requests_enabled=organization_requests_enabled,
        )

    # Platform admins can widen the dashboard to every organization.
    show_all = user.admin and request.args.get("scope") == "all"
    return render_template(
        "main/home.html",
        scope="all" if show_all else "mine",
        organization_requests_enabled=organization_requests_enabled,
        **dashboard_context(user, show_all),
    )


@main_blueprint.route("/about/")
def about() -> str:
    return render_template("main/about.html")
