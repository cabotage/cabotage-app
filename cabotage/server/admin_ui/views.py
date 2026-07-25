import datetime
import re

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_security import current_user
from sqlalchemy import func, or_, true
from sqlalchemy.orm import joinedload

from cabotage.server import db
from cabotage.server.admin_ui.forms import (
    AdminAddOrgMemberForm,
    AdminOrgActionForm,
    AdminOrgMemberActionForm,
    AdminProjectActionForm,
    AdminUserActionForm,
)
from cabotage.server.mfa import get_mfa_status
from cabotage.server.models.auth import Organization, User
from cabotage.server.models.auth_associations import OrganizationMember
from cabotage.server.models.projects import (
    Application,
    Environment,
    Project,
    activity_plugin,
)

Activity = activity_plugin.activity_cls

admin_blueprint = Blueprint("admin", __name__, url_prefix="/admin")

_DELETED_SLUG_RE = re.compile(r"--deleted-[0-9a-f]{12}$")
# Children tombstoned by the same cascade get timestamps a little earlier
# than their parent's; anything outside this window was deleted separately.
_CASCADE_WINDOW = datetime.timedelta(minutes=2)


@admin_blueprint.before_request
def _require_super_admin():
    if not current_user.is_authenticated:
        return current_app.login_manager.unauthorized()
    if not getattr(current_user, "admin", False):
        abort(403)


def _record_activity(verb, obj, **data):
    activity = Activity(
        verb=verb,
        object=obj,
        data={
            "user_id": str(current_user.id),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "via": "admin",
            **data,
        },
    )
    db.session.add(activity)


def _security_datastore():
    return current_app.extensions["security"].datastore


def _display_username(username):
    # Mirrors the display_username Jinja filter.
    if username and username.startswith("github:"):
        parts = username.split(":", 2)
        if len(parts) == 3:
            return parts[2]
    return username


def _confirmed(expected):
    """Server-side check of the typed-confirmation field."""
    if request.form.get("confirm") != expected:
        flash(f"Confirmation did not match '{expected}'; no action taken.", "error")
        return False
    return True


@admin_blueprint.route("/")
def index():
    user_count = User.query.count()
    admin_count = User.query.filter_by(admin=True, active=True).count()
    org_active = Organization.query.filter(Organization.deleted_at.is_(None)).count()
    org_deleted = Organization.query.filter(Organization.deleted_at.isnot(None)).count()
    project_active = Project.query.filter(Project.deleted_at.is_(None)).count()
    project_deleted = Project.query.filter(Project.deleted_at.isnot(None)).count()
    application_count = Application.query.filter(
        Application.deleted_at.is_(None)
    ).count()

    pending_requests = None
    if current_app.config.get("ORGANIZATION_REQUESTS_ENABLED", False):
        from cabotage.server.models.auth import OrganizationRequest

        pending_requests = OrganizationRequest.query.filter_by(
            status=OrganizationRequest.STATUS_PENDING
        ).count()

    return render_template(
        "admin_ui/index.html",
        user_count=user_count,
        admin_count=admin_count,
        org_active=org_active,
        org_deleted=org_deleted,
        project_active=project_active,
        project_deleted=project_deleted,
        application_count=application_count,
        pending_requests=pending_requests,
    )


@admin_blueprint.route("/users")
def users():
    query = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    only_admins = request.args.get("filter") == "admins"

    users_query = User.query
    if only_admins:
        users_query = users_query.filter_by(admin=True)
    if query:
        users_query = users_query.filter(
            or_(
                User.username.ilike(f"%{query}%"),
                User.email.ilike(f"%{query}%"),
            )
        )
    pagination = users_query.order_by(User.registered_at.desc()).paginate(
        page=page, per_page=50, error_out=False
    )
    return render_template(
        "admin_ui/users.html",
        pagination=pagination,
        query=query,
        only_admins=only_admins,
    )


def _abort_if_last_active_admin(user):
    """True (and flashes) when demoting/deactivating would leave no admins."""
    active_admins = (
        User.query.filter_by(admin=True, active=True).with_for_update().all()
    )
    if len(active_admins) <= 1 and any(u.id == user.id for u in active_admins):
        db.session.rollback()
        flash("Refusing: that would leave no active super admins.", "error")
        return True
    return False


@admin_blueprint.route("/users/<uuid:user_id>", methods=["GET", "POST"])
def user_detail(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)

    action_form = AdminUserActionForm()

    if request.method == "POST":
        if not action_form.validate_on_submit():
            abort(400)
        action = request.form.get("_action")

        if action == "grant_admin":
            if user.admin:
                flash(f"{user.username} is already a super admin.", "info")
            else:
                user.admin = True
                _record_activity("grant-admin", user)
                db.session.commit()
                flash(f"{user.username} is now a super admin.", "success")

        elif action == "revoke_admin":
            if user.id == current_user.id:
                flash("You cannot revoke your own admin access.", "error")
            elif not user.admin:
                flash(f"{user.username} is not a super admin.", "info")
            elif not _abort_if_last_active_admin(user):
                user.admin = False
                _record_activity("revoke-admin", user)
                db.session.commit()
                flash(f"Super admin revoked for {user.username}.", "success")

        elif action == "activate":
            if user.active:
                flash(f"{user.username} is already active.", "info")
            else:
                user.active = True
                _record_activity("activate", user)
                db.session.commit()
                flash(f"{user.username} activated.", "success")

        elif action == "deactivate":
            if user.id == current_user.id:
                flash("You cannot deactivate your own account.", "error")
            elif not user.active:
                flash(f"{user.username} is already deactivated.", "info")
            elif not _abort_if_last_active_admin(user):
                user.active = False
                _security_datastore().set_uniquifier(user)
                _record_activity("deactivate", user)
                db.session.commit()
                flash(
                    f"{user.username} deactivated and active sessions revoked.",
                    "success",
                )

        elif action == "reset_mfa":
            if _confirmed(_display_username(user.username)):
                _security_datastore().reset_user_access(user)
                _record_activity("reset-mfa", user)
                db.session.commit()
                flash(
                    f"MFA reset for {user.username}. All sessions are revoked; "
                    "they will re-enroll at next login.",
                    "success",
                )

        else:
            abort(400)
        return redirect(url_for("admin.user_detail", user_id=user.id))

    has_totp, num_webauthn, has_mfa = get_mfa_status(user)
    memberships = (
        OrganizationMember.query.filter_by(user_id=user.id)
        .options(joinedload(OrganizationMember.organization))
        .all()
    )
    return render_template(
        "admin_ui/user_detail.html",
        user=user,
        action_form=action_form,
        has_totp=has_totp,
        num_webauthn=num_webauthn,
        has_mfa=has_mfa,
        memberships=memberships,
    )


@admin_blueprint.route("/organizations")
def organizations():
    member_counts = dict(
        db.session.query(
            OrganizationMember.organization_id, func.count(OrganizationMember.user_id)
        )
        .group_by(OrganizationMember.organization_id)
        .all()
    )
    project_counts = dict(
        db.session.query(Project.organization_id, func.count(Project.id))
        .filter(Project.deleted_at.is_(None))
        .group_by(Project.organization_id)
        .all()
    )
    orgs = Organization.query.order_by(
        Organization.deleted_at.isnot(None), Organization.name
    ).all()
    return render_template(
        "admin_ui/organizations.html",
        organizations=orgs,
        member_counts=member_counts,
        project_counts=project_counts,
    )


def _restored_slug(slug):
    return _DELETED_SLUG_RE.sub("", slug)


def _within_cascade_window(child_deleted_at, parent_deleted_at):
    if child_deleted_at is None or parent_deleted_at is None:
        return False
    return (
        parent_deleted_at - _CASCADE_WINDOW
        <= child_deleted_at
        <= parent_deleted_at + datetime.timedelta(seconds=1)
    )


def _restore_project(project, warnings):
    """Un-tombstone a project and the children deleted by the same cascade.

    Backing-service resources stay deleted; k8s state is not resurrected.
    """
    deleted_at = project.deleted_at
    restored = _restored_slug(project.slug)
    collision = (
        Project.query.filter_by(organization_id=project.organization_id, slug=restored)
        .filter(Project.deleted_at.is_(None))
        .first()
    )
    if collision is not None:
        warnings.append(f"Slug '{restored}' is taken; project not restored.")
        return False

    project.slug = restored
    project.deleted_at = None

    for application in project.project_applications:
        if not _within_cascade_window(application.deleted_at, deleted_at):
            continue
        app_slug = _restored_slug(application.slug)
        app_collision = (
            Application.query.filter_by(project_id=project.id, slug=app_slug)
            .filter(Application.deleted_at.is_(None), Application.id != application.id)
            .first()
        )
        if app_collision is not None:
            warnings.append(f"Application slug '{app_slug}' is taken; left deleted.")
            continue
        application.slug = app_slug
        application.deleted_at = None
        for app_env in application.application_environments:
            if _within_cascade_window(app_env.deleted_at, deleted_at):
                app_env.deleted_at = None

    for environment in project.project_environments:
        if not _within_cascade_window(environment.deleted_at, deleted_at):
            continue
        env_slug = _restored_slug(environment.slug)
        env_collision = (
            Environment.query.filter_by(project_id=project.id, slug=env_slug)
            .filter(Environment.deleted_at.is_(None), Environment.id != environment.id)
            .first()
        )
        if env_collision is not None:
            warnings.append(f"Environment slug '{env_slug}' is taken; left deleted.")
            continue
        environment.slug = env_slug
        environment.deleted_at = None

    return True


@admin_blueprint.route("/organizations/<uuid:org_id>", methods=["GET", "POST"])
def organization_detail(org_id):
    organization = db.session.get(Organization, org_id)
    if organization is None:
        abort(404)

    add_member_form = AdminAddOrgMemberForm()
    member_action_form = AdminOrgMemberActionForm()
    org_action_form = AdminOrgActionForm()

    if request.method == "POST":
        action = request.form.get("_action")

        if action == "add_member":
            if not add_member_form.validate_on_submit():
                abort(400)
            identity = add_member_form.identity.data.strip()
            user = User.query.filter(
                or_(User.email == identity, User.username == identity)
            ).first()
            if user is None:
                flash(f"No user found for '{identity}'.", "error")
            elif any(m.user_id == user.id for m in organization.members):
                flash(f"{user.username} is already a member.", "info")
            else:
                organization.add_user(user, admin=add_member_form.admin.data)
                _record_activity(
                    "add-member",
                    organization,
                    member_user_id=str(user.id),
                    member_username=user.username,
                    org_admin=bool(add_member_form.admin.data),
                )
                db.session.commit()
                flash(f"{user.username} added to {organization.name}.", "success")

        elif action in ("remove_member", "promote_member", "demote_member"):
            if not member_action_form.validate_on_submit():
                abort(400)
            member = OrganizationMember.query.filter_by(
                organization_id=organization.id,
                user_id=member_action_form.user_id.data,
            ).first()
            if member is None:
                flash("That user is not a member of this organization.", "error")
            elif action == "remove_member":
                locked_members = (
                    OrganizationMember.query.filter_by(organization_id=organization.id)
                    .with_for_update()
                    .all()
                )
                if len(locked_members) <= 1:
                    db.session.rollback()
                    flash(
                        "Refusing: organizations must retain at least one member.",
                        "error",
                    )
                else:
                    username = member.user.username
                    _record_activity(
                        "remove-member",
                        organization,
                        member_user_id=str(member.user_id),
                        member_username=username,
                    )
                    db.session.delete(member)
                    db.session.commit()
                    flash(f"{username} removed from {organization.name}.", "success")
            elif action == "promote_member":
                if member.admin:
                    flash(f"{member.user.username} is already an org admin.", "info")
                else:
                    member.admin = True
                    _record_activity(
                        "promote-member",
                        organization,
                        member_user_id=str(member.user_id),
                        member_username=member.user.username,
                    )
                    db.session.commit()
                    flash(f"{member.user.username} is now an org admin.", "success")
            elif action == "demote_member":
                if not member.admin:
                    flash(f"{member.user.username} is not an org admin.", "info")
                else:
                    member.admin = False
                    _record_activity(
                        "demote-member",
                        organization,
                        member_user_id=str(member.user_id),
                        member_username=member.user.username,
                    )
                    db.session.commit()
                    flash(
                        f"Org admin removed for {member.user.username}.",
                        "success",
                    )

        elif action == "soft_delete":
            if not org_action_form.validate_on_submit():
                abort(400)
            if organization.deleted_at is not None:
                flash(f"{organization.name} is already deleted.", "info")
            elif _confirmed(organization.slug):
                from cabotage.server.user.views import _soft_delete_organization

                _soft_delete_organization(organization)
                _record_activity("delete", organization)
                db.session.commit()
                flash(f"Organization {organization.name} deleted.", "success")

        elif action == "restore":
            if not org_action_form.validate_on_submit():
                abort(400)
            if organization.deleted_at is None:
                flash(f"{organization.name} is not deleted.", "info")
            else:
                restored = _restored_slug(organization.slug)
                collision = (
                    Organization.query.filter_by(slug=restored)
                    .filter(Organization.deleted_at.is_(None))
                    .first()
                )
                if collision is not None:
                    flash(
                        f"Slug '{restored}' is taken; organization not restored.",
                        "error",
                    )
                else:
                    org_deleted_at = organization.deleted_at
                    organization.slug = restored
                    organization.deleted_at = None
                    warnings = []
                    for project in organization.projects:
                        if _within_cascade_window(project.deleted_at, org_deleted_at):
                            _restore_project(project, warnings)
                    _record_activity("restore", organization)
                    db.session.commit()
                    for warning in warnings:
                        flash(warning, "warning")
                    flash(
                        f"Organization {organization.name} restored. This is "
                        "database-level only: cluster resources, certificates, and "
                        "integrations are not recreated — applications need "
                        "redeploys and Tailscale must be reconfigured.",
                        "success",
                    )

        else:
            abort(400)
        return redirect(url_for("admin.organization_detail", org_id=organization.id))

    members = (
        OrganizationMember.query.filter_by(organization_id=organization.id)
        .options(joinedload(OrganizationMember.user))
        .all()
    )
    projects = (
        Project.query.filter_by(organization_id=organization.id)
        .order_by(Project.deleted_at.isnot(None), Project.name)
        .all()
    )
    return render_template(
        "admin_ui/organization_detail.html",
        organization=organization,
        members=members,
        projects=projects,
        add_member_form=add_member_form,
        member_action_form=member_action_form,
        org_action_form=org_action_form,
    )


@admin_blueprint.route("/projects")
def projects():
    all_projects = (
        Project.query.options(joinedload(Project.organization))
        .order_by(Project.deleted_at.isnot(None), Project.name)
        .all()
    )
    project_action_form = AdminProjectActionForm()
    return render_template(
        "admin_ui/projects.html",
        projects=all_projects,
        project_action_form=project_action_form,
    )


@admin_blueprint.route("/projects/<uuid:project_id>", methods=["POST"])
def project_action(project_id):
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)

    form = AdminProjectActionForm()
    if not form.validate_on_submit():
        abort(400)
    action = request.form.get("_action")

    if action == "soft_delete":
        if project.deleted_at is not None:
            flash(f"{project.name} is already deleted.", "info")
        elif _confirmed(project.slug):
            from cabotage.server.user.views import _soft_delete_project

            _soft_delete_project(project, project.organization)
            _record_activity("delete", project)
            db.session.commit()
            flash(f"Project {project.name} deleted.", "success")

    elif action == "restore":
        if project.deleted_at is None:
            flash(f"{project.name} is not deleted.", "info")
        elif (
            project.organization is None or project.organization.deleted_at is not None
        ):
            flash(
                "Restore the organization before restoring its projects.",
                "error",
            )
        else:
            warnings = []
            if _restore_project(project, warnings):
                _record_activity("restore", project)
                db.session.commit()
                flash(
                    f"Project {project.name} restored. Database-level only: "
                    "cluster resources are not recreated — applications need "
                    "redeploys and backing services stay deleted.",
                    "success",
                )
            else:
                db.session.rollback()
                for warning in warnings:
                    flash(warning, "error")

    else:
        abort(400)
    referrer = request.referrer or ""
    if referrer.startswith(request.host_url):
        return redirect(referrer)
    return redirect(url_for("admin.projects"))


@admin_blueprint.route("/audit")
def audit_log():
    from cabotage.server.user.views import _render_audit_log

    return _render_audit_log(
        true(),
        {
            "scope_type": "global",
            "audit_url": url_for("admin.audit_log"),
        },
    )
