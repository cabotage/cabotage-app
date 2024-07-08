# manage.py


from urllib.parse import unquote

from flask import url_for
from flask_script import Manager
from flask_migrate import Migrate, MigrateCommand

from cabotage.server import create_app, db
from cabotage.server.models import (
    Organization,
    User,
)
from cabotage.server.models.projects import (
    Application,
    Project,
)


app = create_app()
migrate = Migrate(app, db)
manager = Manager(app)

# migrations
manager.add_command("db", MigrateCommand)


@manager.command
def create_db():
    """Creates the db tables."""
    db.engine.execute("CREATE EXTENSION IF NOT EXISTS citext")
    db.engine.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    db.create_all()


@manager.command
def drop_db():
    """Drops the db tables."""
    db.drop_all()


@manager.command
def create_admin():
    """Creates the admin user."""
    user = User(email="ad@min.com", password="admin", username="admin", admin=True)
    db.session.add(user)
    db.session.flush()
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


@manager.command
def create_data():
    """Creates sample data."""
    pass


@manager.command
def list_routes():
    output = []
    for rule in app.url_map.iter_rules():
        options = {}
        for arg in rule.arguments:
            options[arg] = "[{0}]".format(arg)

        methods = ",".join(rule.methods)
        url = url_for(rule.endpoint, **options)
        line = unquote("{:50s} {:20s} {}".format(rule.endpoint, methods, url))
        output.append(line)

    for line in sorted(output):
        print(line)


if __name__ == "__main__":
    manager.run()
