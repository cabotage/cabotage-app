import os

from flask import Flask, render_template
from flask_bcrypt import Bcrypt
from flask_debugtoolbar import DebugToolbarExtension
from flask_bootstrap import Bootstrap
from flask_security import Security, SQLAlchemyUserDatastore
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail
from flask_migrate import Migrate

from sqlalchemy import MetaData


# instantiate the extensions
bcrypt = Bcrypt()
toolbar = DebugToolbarExtension()
bootstrap = Bootstrap()
security = Security()
db_metadata = MetaData(
    naming_convention={
        "ix": 'ix_%(column_0_label)s',
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)
db = SQLAlchemy(metadata=db_metadata)
mail = Mail()
migrate = Migrate()


def create_app():

    # instantiate the app
    app = Flask(
        __name__,
        template_folder='../client/templates',
        static_folder='../client/static'
    )

    from cabotage.server.models.auth import User, Role
    user_datastore = SQLAlchemyUserDatastore(db, User, Role)

    from cabotage.server.user.forms import (
        ExtendedConfirmRegisterForm,
        ExtendedLoginForm,
        ExtendedRegisterForm,
    )

    from flask_nav import Nav
    from flask_nav.elements import Navbar, View, Separator, Subgroup

    nav = Nav()

    anonymous_nav = Navbar(
        'Cabotage',
        View('Register', 'security.register'),
        View('Log In', 'security.login'),
    )
    logged_in_nav = Navbar(
        'Cabotage',
        Subgroup(
            'Orgs',
            View('All My Orgs', 'user.organizations'),
        ),
        Subgroup(
            'Projects',
            View('All My Projects', 'user.projects'),
        ),
        Subgroup(
          'Account',
          Separator(),
          View('Change Password', 'security.change_password'),
          View('Log Out', 'security.logout'),
        ),
    )
    nav.register_element('anonymous', anonymous_nav)
    nav.register_element('logged_in', logged_in_nav)

    # set config
    app_settings = os.getenv(
        'APP_SETTINGS', 'cabotage.server.config.DevelopmentConfig')
    app.config.from_object(app_settings)

    # set up extensions
    bcrypt.init_app(app)
    toolbar.init_app(app)
    bootstrap.init_app(app)
    security.init_app(
        app, user_datastore,
        confirm_register_form=ExtendedConfirmRegisterForm,
        register_form=ExtendedRegisterForm,
        login_form=ExtendedLoginForm,
    )
    db.init_app(app)
    mail.init_app(app)
    migrate.init_app(app, db)
    nav.init_app(app)

    # register blueprints
    from cabotage.server.user.views import user_blueprint
    from cabotage.server.main.views import main_blueprint
    app.register_blueprint(user_blueprint)
    app.register_blueprint(main_blueprint)

    # error handlers
    @app.errorhandler(401)
    def unauthorized_page(error):
        return render_template('errors/401.html'), 401

    @app.errorhandler(403)
    def forbidden_page(error):
        return render_template('errors/403.html'), 403

    @app.errorhandler(404)
    def page_not_found(error):
        return render_template('errors/404.html'), 404

    @app.errorhandler(500)
    def server_error_page(error):
        return render_template('errors/500.html'), 500

    return app
