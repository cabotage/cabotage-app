from flask_login import current_user
from flask_nav3 import Nav
from flask_nav3.elements import Navbar, View, Separator, Subgroup

nav = Nav()

anonymous_nav = Navbar(
    View("Cabotage", "main.home"),
    View("Log In", "security.login"),
)


def get_logged_in_nav():
    items = [
        Subgroup(
            "Orgs",
            View("All My Orgs", "user.organizations"),
        ),
        Subgroup(
            "Projects",
            View("All My Projects", "user.projects"),
        ),
        Subgroup(
            "Account",
            Separator(),
            View("Change Password", "security.change_password"),
            View("Log Out", "security.logout"),
        ),
    ]

    if hasattr(current_user, "admin") and current_user.admin:
        items.append(
            View("Admin", "admin.index"),
        )

    return Navbar(View("Cabotage", "main.home"), *items)


nav.register_element("anonymous", anonymous_nav)
nav.register_element("logged_in", get_logged_in_nav)
