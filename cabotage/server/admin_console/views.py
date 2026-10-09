"""Platform admin console: inventory, drilldowns and global account controls.

Every route requires an active global admin with a live passkey elevation
(``require_admin_session``). Tenant operations are not reimplemented here:
drilldowns link into the existing organization/project/application handlers,
which honour the elevated ACL. The only mutations owned by the console are
global account controls, each bound to a fresh one-use passkey assertion via
``require_admin_action`` before anything is written.
"""

import uuid
from typing import cast

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from werkzeug.wrappers import Response

from cabotage.server import db
from cabotage.server.admin_console import accounts, queries
from cabotage.server.admin_console.forms import (
    ResetMfaForm,
    SetActiveForm,
    SetAdminForm,
)
from cabotage.server.admin_console.instance import get_instance_application
from cabotage.server.admin_passkey import require_admin_action, require_admin_session
from cabotage.server.models.auth import User
from cabotage.server.user.forms import ReviewOrganizationRequestForm

admin_console_blueprint = Blueprint("admin_console", __name__, url_prefix="/admin")

_ACCOUNT_ENDPOINTS = {
    "admin_console.user_set_active",
    "admin_console.user_set_admin",
    "admin_console.user_reset_mfa",
}


@admin_console_blueprint.before_request
@login_required
def _require_elevation() -> Response | None:
    if (denied := require_admin_session()) is not None:
        return denied
    return None


def user_label(user: User | None) -> str:
    """Same rule as the ``display_username`` template filter."""
    if user is None:
        return ""
    value = (
        cast(str | None, user.username) or cast(str | None, user.email) or str(user.id)
    )
    if value.startswith("github:"):
        parts = value.split(":", 2)
        if len(parts) == 3:
            return parts[2]
    return value


def _requests_enabled() -> bool:
    return bool(
        cast(object, current_app.config.get("ORGANIZATION_REQUESTS_ENABLED", False))
    )


def _action_summary() -> str | None:
    """Plain-language intent for the passkey prompt on console account actions."""
    if request.method != "POST" or request.endpoint not in _ACCOUNT_ENDPOINTS:
        return None
    user_id = (request.view_args or {}).get("user_id")
    target = db.session.get(User, user_id) if user_id else None
    if target is None:
        return None
    name = user_label(target)
    if request.endpoint == "admin_console.user_set_active":
        verb = "Activate" if request.form.get("active") == "true" else "Deactivate"
        return f"{verb} {name}"
    if request.endpoint == "admin_console.user_set_admin":
        if request.form.get("admin") == "true":
            return f"Make {name} a global admin"
        return f"Remove global admin from {name}"
    return f"Reset MFA for {name}"


@admin_console_blueprint.app_template_filter("admin_slug")
def _admin_slug(slug: str | None) -> str:
    return queries.display_slug(slug)


@admin_console_blueprint.context_processor
def _console_context() -> dict[str, object]:
    return {
        "admin_requests_enabled": _requests_enabled(),
        "admin_pending_requests": queries.pending_request_count(),
        "admin_action_summary": _action_summary(),
    }


# ── Argument parsing ────────────────────────────────────────────────────────


def _term() -> str:
    return (request.args.get("q") or "").strip()[: queries.MAX_QUERY_LENGTH]


def _choice(name: str, choices: tuple[str, ...], default: str) -> str:
    value = request.args.get(name, default)
    return value if value in choices else default


def _page() -> int:
    page = request.args.get("page", 1, type=int) or 1
    return min(max(page, 1), queries.MAX_PAGE)


# ── Overview and search ─────────────────────────────────────────────────────


@admin_console_blueprint.get("/")
def index() -> str:
    return render_template(
        "admin_console/index.html",
        active_nav="overview",
        overview=queries.overview(),
        instance_application=get_instance_application(),
        mimir_configured=bool(current_app.config.get("MIMIR_URL")),
        metric_url=url_for("user.infra_observe_metric"),
        current_range=_choice("range", ("1h", "6h", "24h", "7d", "30d"), "1h"),
        current_groups={"cpu": "total", "memory": "total", "network": "total"},
        has_time_window=bool(request.args.get("start") and request.args.get("end")),
    )


@admin_console_blueprint.get("/search")
def search() -> str | Response:
    term = _term()
    if not term:
        return redirect(url_for("admin_console.index"))
    return render_template(
        "admin_console/search.html",
        active_nav="overview",
        term=term,
        results=queries.search(term),
    )


# ── Inventory ───────────────────────────────────────────────────────────────


@admin_console_blueprint.get("/organizations")
def organizations() -> str:
    term, state, page = _term(), _choice("state", queries.STATES, "active"), _page()
    return render_template(
        "admin_console/organizations.html",
        active_nav="organizations",
        term=term,
        state=state,
        page=queries.organizations_page(term, state, page),
    )


@admin_console_blueprint.get("/organizations/<uuid:organization_id>")
def organization(organization_id: uuid.UUID) -> str:
    detail = queries.organization_detail(organization_id)
    if detail is None:
        abort(404)
    return render_template(
        "admin_console/organization.html",
        active_nav="organizations",
        detail=detail,
        org=detail.organization,
    )


@admin_console_blueprint.get("/projects")
def projects() -> str:
    term, state, page = _term(), _choice("state", queries.STATES, "active"), _page()
    return render_template(
        "admin_console/projects.html",
        active_nav="projects",
        term=term,
        state=state,
        page=queries.projects_page(term, state, page),
    )


@admin_console_blueprint.get("/projects/<uuid:project_id>")
def project(project_id: uuid.UUID) -> str:
    detail = queries.project_detail(project_id)
    if detail is None:
        abort(404)
    return render_template(
        "admin_console/project.html",
        active_nav="projects",
        detail=detail,
        project=detail.project,
        org=detail.project.organization,
    )


@admin_console_blueprint.get("/applications")
def applications() -> str:
    term, state, page = _term(), _choice("state", queries.STATES, "active"), _page()
    return render_template(
        "admin_console/applications.html",
        active_nav="applications",
        term=term,
        state=state,
        page=queries.applications_page(term, state, page),
    )


@admin_console_blueprint.get("/applications/<uuid:application_id>")
def application(application_id: uuid.UUID) -> str:
    detail = queries.application_detail(application_id)
    if detail is None:
        abort(404)
    app = detail.application
    return render_template(
        "admin_console/application.html",
        active_nav="applications",
        detail=detail,
        app=app,
        project=app.project,
        org=app.project.organization,
    )


@admin_console_blueprint.get("/requests")
def requests() -> str:
    status = _choice("status", ("pending", "all"), "pending")
    return render_template(
        "admin_console/requests.html",
        active_nav="requests",
        status=status,
        organization_requests=queries.organization_requests(status),
        review_form=ReviewOrganizationRequestForm(),
    )


# ── Users ───────────────────────────────────────────────────────────────────


@admin_console_blueprint.get("/users")
def users() -> str:
    term = _term()
    user_filter = _choice("filter", queries.USER_FILTERS, "all")
    page = _page()
    return render_template(
        "admin_console/users.html",
        active_nav="users",
        term=term,
        user_filter=user_filter,
        page=queries.users_page(term, user_filter, page),
    )


@admin_console_blueprint.get("/users/<uuid:user_id>")
def user(user_id: uuid.UUID) -> str:
    detail = queries.user_detail(user_id)
    if detail is None:
        abort(404)
    target = detail.user
    return render_template(
        "admin_console/user.html",
        active_nav="users",
        detail=detail,
        target=target,
        is_self=target.id == cast(User, current_user).id,
        refusals={
            change: accounts.refusal(
                change, target, cast(User, current_user).id, detail.usable_admins
            )
            for change in (accounts.DEACTIVATE, accounts.DEMOTE, accounts.RESET_MFA)
        },
        active_form=SetActiveForm(),
        admin_form=SetAdminForm(),
        mfa_form=ResetMfaForm(),
    )


def _back_to(user_id: uuid.UUID) -> Response:
    return redirect(url_for("admin_console.user", user_id=user_id))


def _target_or_404(user_id: uuid.UUID) -> User:
    target = db.session.get(User, user_id)
    if target is None:
        abort(404)
    return target


def _refused(change: str, target: User) -> str | None:
    return accounts.refusal(
        change, target, cast(User, current_user).id, queries.usable_admin_ids()
    )


def _invalid(user_id: uuid.UUID) -> Response:
    flash(
        "That form expired or was incomplete. Reload the page and try again.", "error"
    )
    return _back_to(user_id)


@admin_console_blueprint.post("/users/<uuid:user_id>/active")
def user_set_active(user_id: uuid.UUID) -> Response:
    target = _target_or_404(user_id)
    form = SetActiveForm()
    if not form.validate_on_submit():
        return _invalid(user_id)
    active = form.active.data == "true"
    name = user_label(target)
    if target.active == active:
        flash(f"{name} is already {'active' if active else 'inactive'}.", "info")
        return _back_to(user_id)
    if not active and (reason := _refused(accounts.DEACTIVATE, target)):
        flash(reason, "error")
        return _back_to(user_id)

    g.admin_action_summary = {
        "title": "Activate account" if active else "Deactivate account",
        "target": name,
        "consequence": (
            "Allow this user to sign in again. Previously revoked sessions stay revoked."
            if active
            else "Block sign-in and revoke this user's sessions and admin access. "
            "Their organizations and applications are not deleted."
        ),
        "confirm_label": "Activate account" if active else "Deactivate account",
    }
    proof = require_admin_action(
        accounts.ACTION_SET_ACTIVE, str(target.id), {"active": active}
    )
    if proof is not None:
        return proof
    try:
        change = accounts.set_active(target.id, cast(User, current_user).id, active)
    except accounts.AccountActionRefused as refused:
        flash(str(refused), "error")
        return _back_to(user_id)
    except LookupError:
        abort(404)
    if change.changed:
        flash(f"{name} {'activated' if active else 'deactivated'}.", "success")
    else:
        flash(f"{name} was already {'active' if active else 'inactive'}.", "info")
    return _back_to(user_id)


@admin_console_blueprint.post("/users/<uuid:user_id>/admin")
def user_set_admin(user_id: uuid.UUID) -> Response:
    target = _target_or_404(user_id)
    form = SetAdminForm()
    if not form.validate_on_submit():
        return _invalid(user_id)
    admin = form.admin.data == "true"
    name = user_label(target)
    if target.admin == admin:
        flash(f"{name} {'is already' if admin else 'is not'} a global admin.", "info")
        return _back_to(user_id)
    if not admin and (reason := _refused(accounts.DEMOTE, target)):
        flash(reason, "error")
        return _back_to(user_id)

    g.admin_action_summary = {
        "title": "Grant global admin" if admin else "Remove global admin",
        "target": name,
        "consequence": (
            "Grant platform-wide administrative access after passkey verification. "
            "Organization membership does not change."
            if admin
            else "Remove platform-wide administrative access and revoke existing "
            "admin grants. Organization membership does not change."
        ),
        "confirm_label": "Grant global admin" if admin else "Remove global admin",
    }
    proof = require_admin_action(
        accounts.ACTION_SET_ADMIN, str(target.id), {"admin": admin}
    )
    if proof is not None:
        return proof
    try:
        change = accounts.set_admin(target.id, cast(User, current_user).id, admin)
    except accounts.AccountActionRefused as refused:
        flash(str(refused), "error")
        return _back_to(user_id)
    except LookupError:
        abort(404)
    if change.changed:
        flash(
            f"{name} is now a global admin."
            if admin
            else f"Global admin removed from {name}.",
            "success",
        )
    return _back_to(user_id)


@admin_console_blueprint.post("/users/<uuid:user_id>/mfa-reset")
def user_reset_mfa(user_id: uuid.UUID) -> Response:
    target = _target_or_404(user_id)
    form = ResetMfaForm()
    if not form.validate_on_submit():
        return _invalid(user_id)
    typed = (form.confirm.data or "").strip().lower()
    accepted = {
        value.lower()
        for value in (target.username, user_label(target), target.email)
        if value
    }
    if typed not in accepted:
        flash("Confirmation didn't match the username. Nothing was changed.", "error")
        return _back_to(user_id)
    if reason := _refused(accounts.RESET_MFA, target):
        flash(reason, "error")
        return _back_to(user_id)

    g.admin_action_summary = {
        "title": "Reset multi-factor authentication",
        "target": user_label(target),
        "consequence": "Remove this user's passkeys and authenticator enrollment, "
        "revoke their sessions, and require MFA enrollment at next sign-in.",
        "confirm_label": "Reset MFA",
        "confirm_text": user_label(target),
    }
    proof = require_admin_action(
        accounts.ACTION_RESET_MFA, str(target.id), {"reset": "mfa"}
    )
    if proof is not None:
        return proof
    try:
        _ = accounts.reset_mfa(target.id, cast(User, current_user).id)
    except accounts.AccountActionRefused as refused:
        flash(str(refused), "error")
        return _back_to(user_id)
    except LookupError:
        abort(404)
    flash(
        f"MFA reset for {user_label(target)}. They were signed out and must "
        "enroll MFA at next sign-in.",
        "success",
    )
    return _back_to(user_id)
