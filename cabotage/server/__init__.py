import hashlib
import os
from html import escape

import sentry_sdk

try:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import DockerLexer, TextLexer
except ImportError:
    highlight = None
    HtmlFormatter = None
    DockerLexer = None
    TextLexer = None

from flask import Flask, render_template, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_admin import Admin
from flask_babel import Babel
from flask_bcrypt import Bcrypt
from flask_bootstrap import Bootstrap
from flask_debugtoolbar import DebugToolbarExtension
import humanize as humanize_lib
from flask_mail import Mail
from flask_migrate import Migrate
from flask_security import Security, SQLAlchemyUserDatastore
from flask_principal import Principal, identity_loaded
from typing import TYPE_CHECKING

from flask_sqlalchemy import SQLAlchemy
from flask_sock import Sock
from flask_wtf.csrf import CSRFProtect

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
from cabotage.server.mfa import CabotageWebauthnUtil

# instantiate the extensions
bcrypt = Bcrypt()
toolbar = DebugToolbarExtension()
bootstrap = Bootstrap()

security = Security(webauthn_util_cls=CabotageWebauthnUtil)


db_metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)
db: SQLAlchemy = SQLAlchemy(
    metadata=db_metadata, engine_options={"pool_pre_ping": True}
)

if TYPE_CHECKING:
    from flask_sqlalchemy.model import Model
else:
    Model = db.Model
principal = Principal()
mail = Mail()
migrate = Migrate()
consul = Consul()
vault = Vault()
vault_db_creds = VaultDBCreds()
kubernetes = Kubernetes()
config_writer = ConfigWriter(consul=consul, vault=vault)
github_app = GitHubApp()
sock = Sock()
csrf = CSRFProtect()
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
        },
        "stale-build-reaper": {
            "task": "cabotage.celery.tasks.maintain.reap_stale_builds",
            "schedule": 10.0,
            "args": None,
        },
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

    from cabotage.server.models.auth import User, Role, WebAuthn

    user_datastore = SQLAlchemyUserDatastore(db, User, Role, webauthn_model=WebAuthn)

    from cabotage.server.user.forms import (
        ExtendedConfirmRegisterForm,
        ExtendedLoginForm,
        ExtendedRegisterForm,
    )

    # set config
    app_settings = os.getenv("APP_SETTINGS", "cabotage.server.config.Config")
    app.config.from_object(app_settings)

    # TOTP_SECRETS must be a dict — deserialize if loaded as a string from env
    totp_secrets = app.config.get("SECURITY_TOTP_SECRETS")
    if isinstance(totp_secrets, str):
        import json

        app.config["SECURITY_TOTP_SECRETS"] = {
            int(k): v for k, v in json.loads(totp_secrets).items()
        }

    if app.config.get("GITHUB_OAUTH_ONLY"):
        app.config["SECURITY_REGISTERABLE"] = False
        app.config["SECURITY_RECOVERABLE"] = False
        app.config["SECURITY_CHANGEABLE"] = False

    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000  # 1 year; cache-busted by hash

    # Static file cache-busting: append ?v=<hash> to static URLs
    _static_hashes = {}

    def _get_static_hash(filename):
        if filename not in _static_hashes:
            filepath = os.path.join(app.static_folder, filename)
            try:
                with open(filepath, "rb") as f:
                    _static_hashes[filename] = hashlib.md5(
                        f.read(), usedforsecurity=False
                    ).hexdigest()[:12]
            except FileNotFoundError:
                _static_hashes[filename] = None
        return _static_hashes.get(filename)

    def _hashed_url_for(endpoint, **values):
        if endpoint == "static":
            filename = values.get("filename")
            if filename:
                h = _get_static_hash(filename)
                if h:
                    values["v"] = h
        return url_for(endpoint, **values)

    app.jinja_env.globals["url_for"] = _hashed_url_for

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
    from cabotage.server.user.github_oauth import init_github_oauth

    init_github_oauth(app)
    vault_db_creds.init_app(app)
    db.init_app(app)
    principal.init_app(app)
    identity_loaded.connect(cabotage_on_identity_loaded, app)
    mail.init_app(app)
    migrate.init_app(app, db)
    nav.init_app(app)

    @app.template_filter("display_username")
    def display_username_filter(value):
        if value and value.startswith("github:"):
            parts = value.split(":", 2)
            if len(parts) == 3:
                return parts[2]
        return value

    @app.template_filter("humanize")
    def humanize_filter(value):
        return humanize_lib.naturaltime(value)

    @app.template_filter("isoformat_utc")
    def isoformat_utc_filter(value):
        if value is None:
            return ""
        return value.isoformat() + "Z"

    @app.template_filter("duration")
    def duration_filter(obj):
        if not getattr(obj, "created", None) or not getattr(obj, "updated", None):
            return ""
        total = int((obj.updated - obj.created).total_seconds())
        if total < 0:
            return ""
        if total < 60:
            return f"{total}s"
        m, s = divmod(total, 60)
        if m < 60:
            return f"{m}m {s}s" if s else f"{m}m"
        h, m = divmod(m, 60)
        return f"{h}h {m}m"

    @app.template_filter("highlight_code")
    def highlight_code_filter(value, language="text"):
        text = "" if value is None else str(value)
        if not text or text == "None":
            return ""
        if highlight is None:
            return f"<pre>{escape(text)}</pre>"
        lexer = DockerLexer() if language == "dockerfile" else TextLexer()
        formatter = HtmlFormatter(nowrap=False, noclasses=False)
        try:
            return highlight(text, lexer, formatter)
        except Exception:
            return f"<pre>{escape(text)}</pre>"

    consul.init_app(app)
    vault.init_app(app)
    kubernetes.init_app(app)
    config_writer.init_app(app, consul, vault)
    github_app.init_app(app)
    celery_init_app(app)
    csrf.init_app(app)
    sock.init_app(app)
    babel.init_app(app)

    # register blueprints
    from cabotage.server.user.views import user_blueprint
    from cabotage.server.main.views import main_blueprint

    app.register_blueprint(user_blueprint)
    app.register_blueprint(main_blueprint)

    # GitHub webhook uses HMAC validation, not CSRF tokens
    csrf.exempt("cabotage.server.user.views.github_hooks")

    from cabotage.server.mfa import register_mfa_guards

    register_mfa_guards(app)

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
        Ingress,
        IngressHost,
        IngressPath,
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
    admin.add_view(AdminModelView(Ingress, db.session))
    admin.add_view(AdminModelView(IngressHost, db.session))
    admin.add_view(AdminModelView(IngressPath, db.session))
    admin.add_view(AdminModelView(Release, db.session))
    admin.add_view(AdminModelView(Deployment, db.session))
    admin.add_view(AdminModelView(Hook, db.session))
    admin.add_view(AdminModelView(User, db.session))

    num_proxies = app.config.get("PROXY_FIX_NUM_PROXIES", 1)
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=num_proxies,
        x_proto=num_proxies,
        x_host=num_proxies,
        x_prefix=num_proxies,
    )

    original_wsgi = app.wsgi_app

    def _static_cache_headers_middleware(environ, start_response):
        path = environ.get("PATH_INFO", "")
        if not path.startswith("/static/"):
            return original_wsgi(environ, start_response)

        def _filtered_start_response(status, headers, exc_info=None):
            headers = [
                (k, v) for k, v in headers if k.lower() not in ("set-cookie", "vary")
            ]
            return start_response(status, headers, exc_info)

        return original_wsgi(environ, _filtered_start_response)

    app.wsgi_app = _static_cache_headers_middleware

    return app
