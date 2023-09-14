from flask_nav3 import Nav
from flask_nav3.elements import Navbar, View, Separator, Subgroup

nav = Nav()

anonymous_nav = Navbar(
    "Cabotage",
    View("Register", "security.register"),
    View("Log In", "security.login"),
)
logged_in_nav = Navbar(
    "Cabotage",
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
)
nav.register_element("anonymous", anonymous_nav)
nav.register_element("logged_in", logged_in_nav)
