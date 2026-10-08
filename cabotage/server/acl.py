from collections import namedtuple
from functools import partial
from typing import override
from uuid import UUID

from flask import abort, g, has_request_context, request, session
from flask_security import current_user
from flask_principal import Permission, UserNeed, RoleNeed
from sqlalchemy.orm import joinedload

OrganizationNeed = namedtuple("OrganizationNeed", ["method", "value"])
ViewOrganizationNeed = partial(OrganizationNeed, "view")
AdministerOrganizationNeed = partial(OrganizationNeed, "administer")

ProjectNeed = namedtuple("ProjectNeed", ["method", "value"])
ViewProjectNeed = partial(ProjectNeed, "view")
AdministerProjectNeed = partial(ProjectNeed, "administer")

ApplicationNeed = namedtuple("ApplicationNeed", ["method", "value"])
ViewApplicationNeed = partial(ApplicationNeed, "view")
AdministerApplicationNeed = partial(ApplicationNeed, "administer")


def cabotage_on_identity_loaded(sender, identity):
    identity.user = current_user

    if hasattr(current_user, "id"):
        identity.provides.add(UserNeed(current_user.id))

    if hasattr(current_user, "roles"):
        for role in current_user.roles:
            identity.provides.add(RoleNeed(role.name))

    if hasattr(current_user, "id"):
        from cabotage.server.models.auth import Organization
        from cabotage.server.models.auth_associations import OrganizationMember
        from cabotage.server.models.projects import Project

        memberships = (
            OrganizationMember.query.filter_by(user_id=current_user.id)
            .options(
                joinedload(OrganizationMember.organization)
                .joinedload(Organization.projects)
                .joinedload(Project.project_applications)
            )
            .all()
        )
        for membership in memberships:
            identity.provides.add(ViewOrganizationNeed(membership.organization_id))
            if membership.admin:
                identity.provides.add(
                    AdministerOrganizationNeed(membership.organization_id)
                )

            for project in membership.organization.projects:
                identity.provides.add(ViewProjectNeed(project.id))
                if membership.admin:
                    identity.provides.add(AdministerProjectNeed(project.id))

                for application in project.project_applications:
                    identity.provides.add(ViewApplicationNeed(application.id))
                    if membership.admin:
                        identity.provides.add(AdministerApplicationNeed(application.id))


class ElevatedPermission(Permission):
    """Membership first; cross-tenant authority exists only in an elevated request."""

    @override
    def can(self) -> bool:
        if super().can():
            return True
        if not has_request_context():
            return False
        from cabotage.server.admin_passkey import (
            ELEVATED_POST_ENDPOINTS,
            confirm_elevated_get,
            consume_shell_ticket,
            has_admin_session,
            require_admin_session,
            require_elevated_request,
        )

        if not has_admin_session():
            if (
                request.method not in {"GET", "HEAD", "OPTIONS"}
                and current_user.is_authenticated
                and current_user.admin
                and session.get("admin_grant")
            ):
                response = require_admin_session()
                if response is not None:
                    abort(response)
            return False
        g.admin_elevated = True
        # These handlers only redirect; the destination performs authorization and
        # consumes the proof bound to its own URL before doing any work.
        if request.endpoint in {
            "user.project_application_settings_legacy",
            "user.application_release_create_legacy",
            "user.application_images_build_fromsource_legacy",
            "user.application_clear_cache_legacy",
            "user.application_scale_legacy",
            "user.release_deploy_legacy",
        }:
            return True
        if request.endpoint in {
            "user.project_application_shell_socket",
            "user.project_application_shell_socket_env",
        }:
            return consume_shell_ticket()
        if request.method in {"GET", "HEAD"}:
            if request.endpoint in ELEVATED_POST_ENDPOINTS:
                abort(confirm_elevated_get())
        elif request.method != "OPTIONS":
            response = require_elevated_request()
            if response is not None:
                abort(response)
        return True


class ViewOrganizationPermission(ElevatedPermission):
    def __init__(self, organization_id: UUID) -> None:
        need = ViewOrganizationNeed(organization_id)
        super().__init__(need)


class ViewProjectPermission(ElevatedPermission):
    def __init__(self, project_id: UUID) -> None:
        need = ViewProjectNeed(project_id)
        super().__init__(need)


class ViewApplicationPermission(ElevatedPermission):
    def __init__(self, application_id: UUID) -> None:
        need = ViewApplicationNeed(application_id)
        super().__init__(need)


class AdministerOrganizationPermission(ElevatedPermission):
    def __init__(self, organization_id: UUID) -> None:
        need = AdministerOrganizationNeed(organization_id)
        super().__init__(need)


class AdministerProjectPermission(ElevatedPermission):
    def __init__(self, project_id: UUID) -> None:
        need = AdministerProjectNeed(project_id)
        super().__init__(need)


class AdministerApplicationPermission(ElevatedPermission):
    def __init__(self, application_id: UUID) -> None:
        need = AdministerApplicationNeed(application_id)
        super().__init__(need)
