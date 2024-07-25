from cabotage.server import create_app, db
from cabotage.server.models import Organization, User
from cabotage.server.models.projects import Application, Project


app = create_app()

if not app.config["DEBUG"]:
    print("Warning: this command should only be run in development/test environments")
    exit(1)

with app.app_context():
    user = User( # nosec
        email="ad@min.com",
        password="admin",
        username="admin",
        admin=True,
        active=True,
        fs_uniquifier="admin",
    )
    db.session.add(user)
    db.session.flush()

    org = Organization(name="Admin Organization", slug="admin-org")
    org.add_user(user, admin=True)
    db.session.add(org)
    db.session.flush()
    db.session.refresh(org)

    proj = Project(name="Admin Project", slug="admin-proj", organization_id=org.id)
    db.session.add(proj)
    db.session.flush()
    db.session.refresh(proj)

    app = Application(name="Admin Application", slug="admin-app", project_id=proj.id)
    db.session.add(app)

    db.session.commit()
