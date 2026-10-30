from typing import cast

from flask import (
    Blueprint,
    Response,
    current_app,
    has_request_context,
    render_template,
    request,
)
from flask_login import current_user

from cabotage.server.main.clusters import cluster_context
from cabotage.server.main.dashboard import dashboard_context
from cabotage.server.main.legal import legal_home, legal_nav, render_legal_page
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


@main_blueprint.app_context_processor
def inject_legal_links() -> dict[str, object]:
    # Templates rendered outside a request (e.g. emails) have no footer to fill.
    if not has_request_context():
        return {}
    return {"legal_nav": legal_nav(), "legal_home": legal_home()}


@main_blueprint.app_context_processor
def inject_clusters() -> dict[str, object]:
    if not has_request_context():
        return {}
    return dict(cluster_context())


@main_blueprint.route("/_cabotage/ping")
def cluster_ping() -> Response:
    """Reachability probe for other clusters' switchers. No session, no DB."""
    return Response(status=204, headers={"Cache-Control": "no-store"})


@main_blueprint.route("/legal/")
def legal() -> str:
    return render_legal_page("index")


@main_blueprint.route("/legal/terms/")
def terms() -> str:
    return render_legal_page("terms")


@main_blueprint.route("/legal/privacy/")
def privacy() -> str:
    return render_legal_page("privacy")


@main_blueprint.route("/legal/acceptable-use/")
def acceptable_use() -> str:
    return render_legal_page("acceptable_use")
