import hashlib
import os
from html import escape
from typing import Any, ClassVar, TYPE_CHECKING

import sentry_sdk

try:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import DockerLexer, TextLexer
except ImportError:
    highlight: Any = None
    HtmlFormatter: Any = None
    DockerLexer: Any = None
    TextLexer: Any = None

from flask import Flask, render_template, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_admin import Admin
from flask_babel import Babel
from flask_bcrypt import Bcrypt
from flask_debugtoolbar import DebugToolbarExtension
import humanize as humanize_lib
from flask_mail import Mail
from flask_migrate import Migrate
from flask_security import Security, SQLAlchemyUserDatastore
from flask_principal import Principal, identity_loaded

from flask_sqlalchemy import SQLAlchemy
from flask_sock import Sock
from flask_wtf.csrf import CSRFProtect

from celery import Celery
from celery import Task
from celery.schedules import crontab
from sentry_sdk.integrations.flask import FlaskIntegration
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from cabotage.server.acl import cabotage_on_identity_loaded

from cabotage.server.ext.consul import Consul
from cabotage.server.ext.vault import Vault
from cabotage.server.ext.config_writer import ConfigWriter
from cabotage.server.ext.kubernetes import Kubernetes
from cabotage.server.ext.vault_db_creds import VaultDBCreds
from cabotage.server.ext.github_app import GitHubApp
from cabotage.server.mfa import CabotageWebauthnUtil
from cabotage.server.config import validate_tenant_postgres_backup_config

# instantiate the extensions
bcrypt = Bcrypt()
toolbar = DebugToolbarExtension()

security = Security(webauthn_util_cls=CabotageWebauthnUtil)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


db: SQLAlchemy = SQLAlchemy(model_class=Base, engine_options={"pool_pre_ping": True})

if TYPE_CHECKING:

    class Model(Base):
        """Type-checking stub: at runtime this is db.Model which adds query etc."""

        query: ClassVar[Any]
        query_class: ClassVar[type]
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


def _sentry_before_send(event, hint):
    """Filter out the StopIteration raised by flask-sock to signal
    gunicorn that a WebSocket connection has closed (not an error)."""
    exc_info = hint.get("exc_info")
    if exc_info:
        exc_type, exc_value, tb = exc_info
        if exc_type is StopIteration and tb is not None:
            # Walk to the innermost frame
            while tb.tb_next:
                tb = tb.tb_next
            if "simple_websocket" in (tb.tb_frame.f_code.co_filename or ""):
                return None
    return event


sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    integrations=[FlaskIntegration()],
    release=os.getenv("SOURCE_COMMIT"),
    before_send=_sentry_before_send,
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
        "tailscale-state-reconciler": {
            "task": "cabotage.celery.tasks.tailscale.reconcile_tailscale_integration_states",
            "schedule": 30.0,
            "args": None,
        },
        "tailscale-oidc-token-refresh": {
            "task": "cabotage.celery.tasks.tailscale.refresh_tailscale_oidc_tokens",
            "schedule": crontab(minute="*/15"),
            "args": None,
        },
        "job-reaper": {
            "task": "cabotage.celery.tasks.reap_jobs.reap_finished_jobs",
            "schedule": 15.0,
            "args": None,
        },
        "alert-reconciler": {
            "task": "cabotage.celery.tasks.alerting.reconcile_alerts",
            "schedule": 15.0,
            "args": None,
        },
        "notification-reconciler": {
            "task": "cabotage.celery.tasks.notify.reconcile_notifications",
            "schedule": 15.0,
            "args": None,
        },
        "backing-service-reconciler": {
            "task": "cabotage.celery.tasks.resources.reconcile_backing_services",
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

    admin = Admin(name="cabotage_admin", index_view=AdminIndexView())

    from cabotage.server.models.auth import User, Role, WebAuthn

    user_datastore = SQLAlchemyUserDatastore(db, User, Role, webauthn_model=WebAuthn)

    from cabotage.server.user.forms import (
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

    # MetaFlaskEnv parses dotted strings as floats, which truncates IDs
    # like Slack's "10818900810177.1079..." — re-read as raw strings.
    _env_prefix = app.config.get("ENV_PREFIX", "CABOTAGE_")
    for _key in (
        "SLACK_CLIENT_ID",
        "SLACK_CLIENT_SECRET",
        "DISCORD_CLIENT_ID",
        "DISCORD_CLIENT_SECRET",
        "DISCORD_BOT_TOKEN",
    ):
        _raw = os.environ.get(f"{_env_prefix}{_key}")
        if _raw is not None:
            app.config[_key] = _raw

    validate_tenant_postgres_backup_config(app.config)

    if app.config.get("GITHUB_OAUTH_ONLY"):
        app.config["SECURITY_REGISTERABLE"] = False
        app.config["SECURITY_RECOVERABLE"] = False
        app.config["SECURITY_CHANGEABLE"] = False

    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000  # 1 year; cache-busted by hash

    # Static file cache-busting: append ?v=<hash> to static URLs
    _static_hashes = {}

    def _get_static_hash(filename):
        if filename not in _static_hashes:
            if app.static_folder is None:
                return None
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

    app.jinja_env.globals.update(url_for=_hashed_url_for)  # ty: ignore[invalid-argument-type]  # Jinja infers globals as a narrow dict; should be dict[str, Any]

    # set up extensions
    admin.init_app(app)
    bcrypt.init_app(app)
    toolbar.init_app(app)
    security.init_app(
        app,
        user_datastore,
        register_form=ExtendedRegisterForm,
        login_form=ExtendedLoginForm,
    )
    from cabotage.server.user.github_oauth import init_github_oauth
    from cabotage.server.integrations.slack_oauth import init_slack_oauth
    from cabotage.server.integrations.discord_oauth import init_discord_oauth
    from cabotage.server.integrations.notification_routing import (
        init_notification_routing,
    )

    init_github_oauth(app)
    init_slack_oauth(app)
    init_discord_oauth(app)
    init_notification_routing(app)
    vault_db_creds.init_app(app)
    db.init_app(app)
    principal.init_app(app)
    identity_loaded.connect(cabotage_on_identity_loaded, app)

    from cabotage.server.audit import init_audit

    init_audit(app)

    mail.init_app(app)
    migrate.init_app(app, db)

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

    @app.template_filter("timeago")
    def timeago_filter(value):
        """Server-side timeago matching the JS timeago() function exactly."""
        if value is None:
            return ""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        diff = max(0, int((now - value).total_seconds()))
        if diff < 2:
            return "just now"
        if diff < 60:
            return f"{diff} seconds ago"
        m = diff // 60
        if m == 1:
            return "a minute ago"
        if m < 60:
            return f"{m} minutes ago"
        h = m // 60
        if h == 1:
            return "an hour ago"
        if h < 24:
            return f"{h} hours ago"
        d = h // 24
        if d == 1:
            return "a day ago"
        return f"{d} days ago"

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
        if (
            highlight is None
            or DockerLexer is None
            or TextLexer is None
            or HtmlFormatter is None
        ):
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
    from cabotage.server.oidc.views import oidc_blueprint
    from cabotage.server.registry_auth.views import registry_auth_blueprint
    from cabotage.server.alerting.views import alerting_blueprint

    app.register_blueprint(user_blueprint)
    app.register_blueprint(main_blueprint)
    app.register_blueprint(oidc_blueprint)
    app.register_blueprint(registry_auth_blueprint)
    app.register_blueprint(alerting_blueprint)

    # GitHub webhook uses HMAC validation, not CSRF tokens
    csrf.exempt("cabotage.server.user.views.github_hooks")
    # Alertmanager webhook uses bearer token auth, not CSRF tokens
    csrf.exempt("cabotage.server.alerting.views.alertmanager_webhook")

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
        Alert,
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
    admin.add_view(AdminModelView(Alert, db.session))
    admin.add_view(AdminModelView(User, db.session))

    num_proxies = app.config.get("PROXY_FIX_NUM_PROXIES", 1)
    app.wsgi_app = ProxyFix(  # ty: ignore[invalid-assignment]  # Flask types wsgi_app as a method but documents reassignment
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

    app.wsgi_app = _static_cache_headers_middleware  # ty: ignore[invalid-assignment]  # Flask types wsgi_app as a method but documents reassignment

    return app
