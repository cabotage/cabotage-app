import os

from flask_env import MetaFlaskEnv
from flask_security import uia_username_mapper, uia_email_mapper

basedir = os.path.abspath(os.path.dirname(__file__))


class Config(metaclass=MetaFlaskEnv):
    ENV_PREFIX = "CABOTAGE_"
    ENV_LOAD_ALL = True

    EXT_SERVER_NAME = "cabotage-app:8000"
    EXT_PREFERRED_URL_SCHEME = "http"

    FLASK_ADMIN_SWATCH = "cerulean"
    SECRET_KEY = "my_precious"  # nosec
    DEBUG = False
    MAX_CONTENT_LENGTH = 32 * 1024 * 1024
    BCRYPT_LOG_ROUNDS = 13
    WTF_CSRF_ENABLED = True
    DEBUG_TB_ENABLED = False
    DEBUG_TB_INTERCEPT_REDIRECTS = False
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECURITY_PASSWORD_SALT = "my_precious"  # nosec
    SECURITY_TRACKABLE = True
    SECURITY_CHANGEABLE = True
    SECURITY_CONFIRMABLE = True
    SECURITY_REGISTERABLE = True
    SECURITY_RECOVERABLE = True
    SECURITY_EMAIL_SENDER = "noreply@localhost"
    SECURITY_TOTP_SECRETS = {1: "my_precious"}
    SECURITY_TOTP_ISSUER = "cabotage"
    REQUIRE_MFA = True
    SECURITY_TWO_FACTOR = True
    SECURITY_TWO_FACTOR_ALWAYS_VALIDATE = False
    SECURITY_TWO_FACTOR_LOGIN_VALIDITY = "30 days"
    SECURITY_TWO_FACTOR_ENABLED_METHODS = ["authenticator"]
    SECURITY_TWO_FACTOR_POST_SETUP_VIEW = "/account/security"
    SECURITY_MULTI_FACTOR_RECOVERY_CODES = True
    SECURITY_MULTI_FACTOR_RECOVERY_CODES_N = 10
    SECURITY_WEBAUTHN = True
    SECURITY_WAN_ALLOW_AS_FIRST_FACTOR = True
    SECURITY_WAN_ALLOW_AS_MULTI_FACTOR = True
    SECURITY_WAN_POST_REGISTER_VIEW = "/account/security"
    SECURITY_UNIFIED_SIGNIN = True
    SECURITY_US_SIGNIN_REPLACES_LOGIN = True
    SECURITY_US_ENABLED_METHODS = ["password"]
    SECURITY_USER_IDENTITY_ATTRIBUTES = [
        {"email": {"mapper": uia_email_mapper, "case_insensitive": True}},
        {"username": {"mapper": uia_username_mapper, "case_insensitive": True}},
    ]
    SECURITY_USERNAME_ENABLE = True
    SECURITY_USERNAME_MIN_LENGTH = 2
    SECURITY_POST_REGISTER_VIEW = "security.login"
    MAIL_SERVER = "app.debugmail.io"
    MAIL_PORT = 25
    MAIL_USE_TLS = False
    MAIL_USE_SSL = False
    MAIL_USERNAME = "ewdurbin@gmail.com"
    MAIL_PASSWORD = "40d5a170-f648-11e7-8c0d-e34c4746c3e2"  # nosec
    MAIL_DEFAULT_SENDER = "noreply@localhost"
    BOOTSTRAP_SERVE_LOCAL = True
    HUMANIZE_USE_UTC = True
    WRITE_BACKENDS = True
    CONSUL_HOST = "consul"
    CONSUL_PORT = 8500
    CONSUL_SCHEME = "http"
    CONSUL_VERIFY = False
    CONSUL_CERT = None
    CONSUL_PREFIX = "cabotage"
    VAULT_TOKEN = None
    VAULT_TOKEN_UNWRAP = False
    VAULT_URL = "http://vault:8200"
    VAULT_VERIFY = False
    VAULT_CERT = None
    VAULT_PREFIX = "cabotage-secrets"
    VAULT_SIGNING_MOUNT = "transit"
    VAULT_SIGNING_KEY = "cabotage-app"
    REGISTRY_BUILD = "registry:5001"
    REGISTRY_PULL = "registry:5001"
    REGISTRY_SECURE = False
    REGISTRY_VERIFY = False
    REGISTRY_AUTH_SECRET = "v3rys3cur3"  # nosec
    BUILDKITD_URL = "tcp://cabotage-buildkitd:1234"
    BUILDKITD_VERIFY = None
    BUILDKIT_IMAGE = "moby/buildkit:v0.28.0-rootless"
    CELERY_BROKER_URL = "redis://redis:6379"
    CELERY_RESULT_BACKEND = "redis://redis:6379"
    KUBERNETES_ENABLED = False
    KUBERNETES_CONTEXT = "cabotage"
    GITHUB_APP_ID = None
    GITHUB_APP_PRIVATE_KEY = None
    GITHUB_WEBHOOK_SECRET = None
    GITHUB_TOKEN = None
    GITHUB_APP_CLIENT_ID = None
    GITHUB_APP_CLIENT_SECRET = None
    GITHUB_OAUTH_ONLY = False
    GITHUB_OAUTH_ALLOWED_ORGS = None
    SHELLZ_ENABLED = False
    SOCK_SERVER_OPTIONS = {"ping_interval": 25}
    SIDECAR_IMAGE = "cabotage/sidecar:3"
    DATADOG_IMAGE = "datadog/agent:7.55.2"
    INGRESS_DOMAIN = None
    TAILSCALE_OPERATOR_IMAGE = "ghcr.io/tailscale/k8s-operator"
    TAILSCALE_OPERATOR_VERSION = "v1.94.2"
    TAILSCALE_OPERATOR_ENABLED = False
    MIMIR_URL = None
    MIMIR_VERIFY = None
    LOKI_URL = None
    LOKI_VERIFY = None
    PROXY_FIX_NUM_PROXIES = 1
