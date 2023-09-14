from collections import namedtuple
from functools import partial

from flask_security import current_user
from flask_principal import Permission, UserNeed, RoleNeed

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
        for organization in current_user.organizations:
            identity.provides.add(ViewOrganizationNeed(organization.organization_id))
            if organization.admin:
                identity.provides.add(
                    AdministerOrganizationNeed(organization.organization_id)
                )

            for project in organization.organization.projects:
                identity.provides.add(ViewProjectNeed(project.id))
                if organization.admin:
                    identity.provides.add(AdministerProjectNeed(project.id))

                for application in project.project_applications:
                    identity.provides.add(ViewApplicationNeed(application.id))
                    if organization.admin:
                        identity.provides.add(AdministerApplicationNeed(application.id))


class ViewOrganizationPermission(Permission):
    def __init__(self, organization_id):
        need = ViewOrganizationNeed(organization_id)
        super().__init__(need)


class ViewProjectPermission(Permission):
    def __init__(self, project_id):
        need = ViewProjectNeed(project_id)
        super().__init__(need)


class ViewApplicationPermission(Permission):
    def __init__(self, application_id):
        need = ViewApplicationNeed(application_id)
        super().__init__(need)


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
