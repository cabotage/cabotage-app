import os

import sentry_sdk

from flask import Flask, render_template
from flask_admin import Admin
from flask_babel import Babel
from flask_bcrypt import Bcrypt
from flask_bootstrap import Bootstrap
from flask_debugtoolbar import DebugToolbarExtension
from flask_humanize import Humanize
from flask_login import LoginManager
from flask_mail import Mail
from flask_migrate import Migrate
from flask_security import Security, SQLAlchemyUserDatastore
from flask_principal import Principal, identity_loaded
from flask_sqlalchemy import SQLAlchemy
from flask_sock import Sock

from celery import Celery
from celery import Task
from celery.schedules import crontab
from sentry_sdk.integrations.flask import FlaskIntegration
from sqlalchemy import MetaData

from cabotage.server.acl import cabotage_on_identity_loaded
from cabotage.server.nav import nav

from cabotage.server.ext.consul import Consul
from cabotage.server.ext.vault import Vault
from cabotage.server.ext.config_writer import ConfigWriter
from cabotage.server.ext.kubernetes import Kubernetes
from cabotage.server.ext.vault_db_creds import VaultDBCreds
from cabotage.server.ext.github_app import GitHubApp

# instantiate the extensions
bcrypt = Bcrypt()
toolbar = DebugToolbarExtension()
bootstrap = Bootstrap()
security = Security()
db_metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)
db: SQLAlchemy = SQLAlchemy(metadata=db_metadata, engine_options={"pool_pre_ping": True})
principal = Principal()
login_manager = LoginManager()
mail = Mail()
migrate = Migrate()
humanize = Humanize()
consul = Consul()
vault = Vault()
vault_db_creds = VaultDBCreds()
kubernetes = Kubernetes()
config_writer = ConfigWriter(consul=consul, vault=vault)
github_app = GitHubApp()
sock = Sock()
babel = Babel()
sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    integrations=[FlaskIntegration()],
    release=os.getenv("SOURCE_COMMIT"),
)


def celery_init_app(app):
    class FlaskTask(Task):
        def __call__(self, *args: object, **kwargs: object) -> object:
            with app.app_context():
                return self.run(*args, **kwargs)

    celery_app = Celery(
        app.name, broker=app.config["CELERY_BROKER_URL"], task_cls=FlaskTask
    )
    celery_app.set_default()
    celery_app.conf.beat_schedule = {
        "pod-reaper": {
            "task": "cabotage.celery.tasks.maintain.reap_pods",
            "schedule": crontab(minute="*/5"),
            "args": None,
        }
    }
    app.extensions["celery"] = celery_app
    return celery_app


def create_app():
    # instantiate the app
    app = Flask(
        __name__,
        template_folder="../client/templates",
        static_folder="../client/static",
    )

    from cabotage.server.models.admin import AdminIndexView

    admin = Admin(
        name="cabotage_admin", index_view=AdminIndexView(), template_mode="bootstrap3"
    )

    from cabotage.server.models.auth import User, Role

    user_datastore = SQLAlchemyUserDatastore(db, User, Role)

    from cabotage.server.user.forms import (
        ExtendedConfirmRegisterForm,
        ExtendedLoginForm,
        ExtendedRegisterForm,
    )

    # set config
    app_settings = os.getenv("APP_SETTINGS", "cabotage.server.config.Config")
    app.config.from_object(app_settings)

    # set up extensions
    admin.init_app(app)
    bcrypt.init_app(app)
    toolbar.init_app(app)
    bootstrap.init_app(app)
    security.init_app(
        app,
        user_datastore,
        confirm_register_form=ExtendedConfirmRegisterForm,
        register_form=ExtendedRegisterForm,
        login_form=ExtendedLoginForm,
    )
    vault_db_creds.init_app(app)
    db.init_app(app)
    principal.init_app(app)
    identity_loaded.connect(cabotage_on_identity_loaded, app)
    login_manager.init_app(app)
    mail.init_app(app)
    migrate.init_app(app, db)
    nav.init_app(app)
    humanize.init_app(app)
    consul.init_app(app)
    vault.init_app(app)
    kubernetes.init_app(app)
    config_writer.init_app(app, consul, vault)
    github_app.init_app(app)
    celery_init_app(app)
    sock.init_app(app)
    babel.init_app(app)

    @login_manager.user_loader
    def load_user(userid):
        return user_datastore.find_user(id=userid)

    # register blueprints
    from cabotage.server.user.views import user_blueprint
    from cabotage.server.main.views import main_blueprint

    app.register_blueprint(user_blueprint)
    app.register_blueprint(main_blueprint)

    # error handlers
    @app.errorhandler(401)
    def unauthorized_page(error):
        return render_template("errors/401.html"), 401

    @app.errorhandler(403)
    def forbidden_page(error):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def page_not_found(error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error_page(error):
        return render_template("errors/500.html"), 500

    from cabotage.server.models.admin import AdminModelView
    from cabotage.server.models.auth import Organization, Team
    from cabotage.server.models.projects import (
        Project,
        Application,
        Configuration,
        Image,
        Release,
        Deployment,
        Hook,
    )

    admin.add_view(AdminModelView(Role, db.session))
    admin.add_view(AdminModelView(Organization, db.session))
    admin.add_view(AdminModelView(Team, db.session))
    admin.add_view(AdminModelView(Project, db.session))
    admin.add_view(AdminModelView(Application, db.session))
    admin.add_view(AdminModelView(Configuration, db.session))
    admin.add_view(AdminModelView(Image, db.session))
    admin.add_view(AdminModelView(Release, db.session))
    admin.add_view(AdminModelView(Deployment, db.session))
    admin.add_view(AdminModelView(Hook, db.session))
    admin.add_view(AdminModelView(User, db.session))

    return app
