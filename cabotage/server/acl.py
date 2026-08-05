from collections import namedtuple
from functools import partial

from flask_security import current_user
from flask_principal import Permission, UserNeed, RoleNeed
from sqlalchemy.orm import joinedload

OrganizationNeed = namedtuple("OrganizationNeed", ["method", "value"])
ViewOrganizationNeed = partial(OrganizationNeed, "view")
AdministerOrganizationNeed = partial(OrganizationNeed, "administer")

# Sentinel need granted to super admins (User.admin). Carried by the View*
# permission classes only, so super admins can see everything but mutations
# gated on Member* or Administer* permissions still require membership.
AdminNeed = namedtuple("AdminNeed", ["value"])
AdminViewAllNeed = AdminNeed("view-all")

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

    if getattr(current_user, "admin", False):
        identity.provides.add(AdminViewAllNeed)

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


# MemberView* carry only the membership-derived need — use them to gate
# mutations. View* extend them with the super-admin sentinel, granting
# read access everywhere.
class MemberViewOrganizationPermission(Permission):
    def __init__(self, organization_id):
        need = ViewOrganizationNeed(organization_id)
        super().__init__(need)


class MemberViewProjectPermission(Permission):
    def __init__(self, project_id):
        need = ViewProjectNeed(project_id)
        super().__init__(need)


class MemberViewApplicationPermission(Permission):
    def __init__(self, application_id):
        need = ViewApplicationNeed(application_id)
        super().__init__(need)


class ViewOrganizationPermission(MemberViewOrganizationPermission):
    def __init__(self, organization_id):
        super().__init__(organization_id)
        self.needs.add(AdminViewAllNeed)


class ViewProjectPermission(MemberViewProjectPermission):
    def __init__(self, project_id):
        super().__init__(project_id)
        self.needs.add(AdminViewAllNeed)


class ViewApplicationPermission(MemberViewApplicationPermission):
    def __init__(self, application_id):
        super().__init__(application_id)
        self.needs.add(AdminViewAllNeed)


def is_direct_org_member(organization_id):
    """True when the current identity carries actual membership in the org,
    as opposed to seeing it through super-admin view access."""
    return MemberViewOrganizationPermission(organization_id).can()


class AdministerOrganizationPermission(Permission):
    def __init__(self, organization_id):
        need = AdministerOrganizationNeed(organization_id)
        super().__init__(need)


class AdministerProjectPermission(Permission):
    def __init__(self, project_id):
        need = AdministerProjectNeed(project_id)
        super().__init__(need)


class AdministerApplicationPermission(Permission):
    def __init__(self, application_id):
        need = AdministerApplicationNeed(application_id)
        super().__init__(need)
