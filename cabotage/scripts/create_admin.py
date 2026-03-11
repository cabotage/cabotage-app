from cabotage.server import create_app, db
from cabotage.server.models import Organization, User
from cabotage.server.models.projects import Application, ApplicationEnvironment, Environment, Project


app = create_app()

if not app.config["DEBUG"]:
    print("Warning: this command should only be run in development/test environments")
    exit(1)

with app.app_context():
    user = User(  # nosec
        email="ad@min.com",
        password="admin",
        username="admin",
        admin=True,
        active=True,
        fs_uniquifier="admin",
    )
    db.session.add(user)
    db.session.flush()

    org = Organization(name="Acme Corp", slug="acme-corp")
    org.add_user(user, admin=True)
    db.session.add(org)
    db.session.flush()
    db.session.refresh(org)

    proj = Project(name="My API", slug="my-api", organization_id=org.id, environments_enabled=True)
    db.session.add(proj)
    db.session.flush()
    db.session.refresh(proj)

    env = Environment(name="Production", slug="production", project_id=proj.id, is_default=True)
    db.session.add(env)
    db.session.flush()
    db.session.refresh(env)

    application = Application(name="Web", slug="web", project_id=proj.id)
    db.session.add(application)
    db.session.flush()
    db.session.refresh(application)

    app_env = ApplicationEnvironment(
        application_id=application.id,
        environment_id=env.id,
        k8s_identifier=env.k8s_identifier,
    )
    db.session.add(app_env)

    db.session.commit()
