import os

from flask_env import MetaFlaskEnv
from flask_security import uia_username_mapper, uia_email_mapper

basedir = os.path.abspath(os.path.dirname(__file__))


def validate_tenant_postgres_backup_config(config):
    if not config.get("TENANT_POSTGRES_BACKUPS_ENABLED"):
        return

    provider = str(config.get("TENANT_POSTGRES_BACKUP_PROVIDER") or "").strip().lower()
    if provider not in {"s3", "rustfs"}:
        raise ValueError(
            "TENANT_POSTGRES_BACKUP_PROVIDER must be 's3' or 'rustfs' when "
            "TENANT_POSTGRES_BACKUPS_ENABLED is true"
        )

    required_keys = [
        "TENANT_POSTGRES_BACKUP_BUCKET",
        "TENANT_POSTGRES_BACKUP_PATH_PREFIX",
        "TENANT_POSTGRES_BACKUP_PLUGIN_NAME",
        "TENANT_POSTGRES_BACKUP_RETENTION_POLICY",
        "TENANT_POSTGRES_BACKUP_SCHEDULE",
        "TENANT_POSTGRES_BACKUP_SERVICE_ACCOUNT_NAME",
    ]
    if provider == "s3":
        required_keys.append("TENANT_POSTGRES_BACKUP_IRSA_ROLE_ARN")
    else:
        required_keys.extend(
            [
                "TENANT_POSTGRES_BACKUP_RUSTFS_ENDPOINT",
                "TENANT_POSTGRES_BACKUP_RUSTFS_CA_SECRET_NAME",
                "TENANT_POSTGRES_BACKUP_RUSTFS_SECRET_NAME",
                "TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAME",
                "TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAMESPACE",
            ]
        )

    missing = [key for key in required_keys if not config.get(key)]
    if missing:
        raise ValueError(
            "Tenant Postgres backups are enabled, but required config is missing: "
            + ", ".join(sorted(missing))
        )


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
    DOCKERHUB_USERNAME = None
    DOCKERHUB_TOKEN = None
    BUILDKITD_URL = "tcp://cabotage-buildkitd:1234"
    BUILDKITD_VERIFY = None
    BUILDKIT_IMAGE = "moby/buildkit:v0.28.0-rootless"
    CELERY_BROKER_URL = "redis://redis:6379"
    CELERY_RESULT_BACKEND = "redis://redis:6379"
    KUBERNETES_ENABLED = False
    KUBERNETES_CONTEXT = "cabotage"
    NETWORK_POLICIES_ENABLED = False
    BACKING_SERVICE_POSTGRES_ENABLED = False
    BACKING_SERVICE_REDIS_ENABLED = False
    BACKING_SERVICES_POOL = None
    TENANT_POSTGRES_BACKUPS_ENABLED = False
    TENANT_POSTGRES_BACKUP_PROVIDER = None
    TENANT_POSTGRES_BACKUP_BUCKET = None
    TENANT_POSTGRES_BACKUP_IRSA_ROLE_ARN = None
    TENANT_POSTGRES_BACKUP_PATH_PREFIX = "tenants"
    TENANT_POSTGRES_BACKUP_PLUGIN_NAME = "barman-cloud.cloudnative-pg.io"
    TENANT_POSTGRES_BACKUP_RETENTION_POLICY = "30d"
    TENANT_POSTGRES_BACKUP_SCHEDULE = "0 0 0 * * *"
    TENANT_POSTGRES_BACKUP_SERVICE_ACCOUNT_NAME = "cnpg-backups"
    TENANT_POSTGRES_BACKUP_RUSTFS_ENDPOINT = None
    TENANT_POSTGRES_BACKUP_RUSTFS_CA_SECRET_NAME = "operators-ca-crt"  # nosec B105
    TENANT_POSTGRES_BACKUP_RUSTFS_SECRET_NAME = "cnpg-backups-objectstore"  # nosec B105
    TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAME = None
    TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAMESPACE = None
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
    SIDECAR_IMAGE = "ghcr.io/cabotage/containers/sidecar-rs:1.0"
    DATADOG_IMAGE = "datadog/agent:7.78.0"
    INGRESS_DOMAIN = None
    TAILSCALE_OPERATOR_ENABLED = False
    TAILSCALE_TAG_PREFIX = "cabotage"
    MIMIR_TENANT_ID = "cabotage-infra"
    MIMIR_TIMEOUT = 5
    MIMIR_URL = None
    MIMIR_VERIFY = None
    LOKI_LEGACY_TENANT_ID = "fake"
    LOKI_URL = None
    LOKI_VERIFY = None
    SLACK_CLIENT_ID = None
    SLACK_CLIENT_SECRET = None
    DISCORD_CLIENT_ID = None
    DISCORD_CLIENT_SECRET = None
    DISCORD_BOT_TOKEN = None
    ALERTMANAGER_WEBHOOK_SECRET = None
    ALERTMANAGER_URL = None
    ALERTMANAGER_VERIFY = None
    PROXY_FIX_NUM_PROXIES = 1
