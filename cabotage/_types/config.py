from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import TypedDict, NotRequired
    from collections.abc import Callable

    class IdentityAttributes(TypedDict):
        mapper: Callable[[str], str | None]
        case_insensitive: bool

    class ServerOption(TypedDict):
        ping_interval: int

    class ConfigDict(TypedDict):
        ALERTMANAGER_URL: NotRequired[str]
        ALERTMANAGER_VERIFY: NotRequired[bool]
        ALERTMANAGER_WEBHOOK_SECRET: NotRequired[str]
        BACKING_SERVICE_POSTGRES_ENABLED: bool
        BACKING_SERVICE_REDIS_ENABLED: bool
        BACKING_SERVICES_POOL: NotRequired[str]
        BCRYPT_LOG_ROUNDS: int
        BOOTSTRAP_SERVE_LOCAL: bool
        BUILDKIT_IMAGE: str
        BUILDKITD_URL: str
        BUILDKITD_VERIFY: None
        CELERY_BROKER_URL: str
        CELERY_RESULT_BACKEND: str
        CONSUL_CERT: None
        CONSUL_HOST: str
        CONSUL_PORT: int
        CONSUL_PREFIX: str
        CONSUL_SCHEME: str
        CONSUL_TOKEN_FILE: NotRequired[str]
        CONSUL_TOKEN: NotRequired[str]
        CONSUL_VERIFY: bool
        DATADOG_IMAGE: str
        DEBUG_TB_ENABLED: bool
        DEBUG_TB_INTERCEPT_REDIRECTS: bool
        DEBUG: bool
        DISCORD_BOT_TOKEN: NotRequired[str]
        DISCORD_CLIENT_ID: NotRequired[str]
        DISCORD_CLIENT_SECRET: NotRequired[str]
        DOCKERHUB_TOKEN: NotRequired[str]
        DOCKERHUB_USERNAME: NotRequired[str]
        ENV_PREFIX: NotRequired[str]
        EXT_PREFERRED_URL_SCHEME: str
        EXT_SERVER_NAME: str
        FLASK_ADMIN_SWATCH: str
        GITHUB_APP_CLIENT_ID: NotRequired[str]
        GITHUB_APP_CLIENT_SECRET: NotRequired[str]
        GITHUB_APP_ID: NotRequired[str]
        GITHUB_APP_INSTALL_URL: NotRequired[str]
        GITHUB_APP_PRIVATE_KEY: NotRequired[str]
        GITHUB_APP_URL: NotRequired[str]
        GITHUB_OAUTH_ALLOWED_ORGS: NotRequired[str]
        GITHUB_OAUTH_ONLY: bool
        GITHUB_TOKEN: NotRequired[str]
        GITHUB_WEBHOOK_SECRET: str
        HUMANIZE_USE_UTC: bool
        INGRESS_DOMAIN: NotRequired[str]
        KUBERNETES_BUILD_NAMESPACE: str
        KUBERNETES_CONTEXT: str
        KUBERNETES_ENABLED: bool
        LOKI_LEGACY_TENANT_ID: str
        LOKI_URL: NotRequired[str]
        LOKI_VERIFY: NotRequired[bool | str]
        MAIL_DEFAULT_SENDER: str
        MAIL_PASSWORD: str
        MAIL_PORT: int
        MAIL_SERVER: str
        MAIL_USE_SSL: bool
        MAIL_USE_TLS: bool
        MAIL_USERNAME: str
        MAX_CONTENT_LENGTH: int
        MIMIR_TENANT_ID: str
        MIMIR_TIMEOUT: int
        MIMIR_URL: NotRequired[str]
        MIMIR_VERIFY: NotRequired[bool | str]
        NETWORK_POLICIES_ENABLED: bool
        ORGANIZATION_REQUESTS_ENABLED: bool
        PROXY_FIX_NUM_PROXIES: int
        REGISTRY_AUTH_SECRET: str
        REGISTRY_BUILD: str
        REGISTRY_PULL: str
        REGISTRY_SECURE: bool
        REGISTRY_VERIFY: bool
        REQUIRE_MFA: bool
        SECRET_KEY: str
        SECURITY_CHANGEABLE: bool
        SECURITY_CONFIRMABLE: bool
        SECURITY_EMAIL_SENDER: str
        SECURITY_MULTI_FACTOR_RECOVERY_CODES_N: int
        SECURITY_MULTI_FACTOR_RECOVERY_CODES: bool
        SECURITY_PASSWORD_SALT: str
        SECURITY_POST_REGISTER_VIEW: str
        SECURITY_RECOVERABLE: bool
        SECURITY_REGISTERABLE: bool
        SECURITY_TOTP_ISSUER: str
        SECURITY_TOTP_SECRETS: dict[int, str] | str
        SECURITY_TRACKABLE: bool
        SECURITY_TWO_FACTOR_ALWAYS_VALIDATE: bool
        SECURITY_TWO_FACTOR_ENABLED_METHODS: list[str]
        SECURITY_TWO_FACTOR_LOGIN_VALIDITY: str
        SECURITY_TWO_FACTOR_POST_SETUP_VIEW: str
        SECURITY_TWO_FACTOR: bool
        SECURITY_UNIFIED_SIGNIN: bool
        SECURITY_US_ENABLED_METHODS: list[str]
        SECURITY_US_SIGNIN_REPLACES_LOGIN: bool
        SECURITY_USER_IDENTITY_ATTRIBUTES: list[dict[str, IdentityAttributes]]
        SECURITY_USERNAME_ENABLE: bool
        SECURITY_USERNAME_MIN_LENGTH: int
        SECURITY_WAN_ALLOW_AS_FIRST_FACTOR: bool
        SECURITY_WAN_ALLOW_AS_MULTI_FACTOR: bool
        SECURITY_WAN_POST_REGISTER_VIEW: str
        SECURITY_WEBAUTHN: bool
        SEND_FILE_MAX_AGE_DEFAULT: NotRequired[int]
        SHELLZ_ENABLED: bool
        SIDECAR_IMAGE: str
        SLACK_CLIENT_ID: NotRequired[str]
        SLACK_CLIENT_SECRET: NotRequired[str]
        SOCK_SERVER_OPTIONS: ServerOption
        SQLALCHEMY_DATABASE_URI: NotRequired[str]
        SQLALCHEMY_TRACK_MODIFICATIONS: bool
        TAILSCALE_OPERATOR_ENABLED: bool
        TAILSCALE_TAG_PREFIX: str
        TENANT_POSTGRES_BACKUP_BUCKET: NotRequired[str]
        TENANT_POSTGRES_BACKUP_IRSA_ROLE_ARN: NotRequired[str]
        TENANT_POSTGRES_BACKUP_PATH_PREFIX: str
        TENANT_POSTGRES_BACKUP_PLUGIN_NAME: str
        TENANT_POSTGRES_BACKUP_PROVIDER: NotRequired[str]
        TENANT_POSTGRES_BACKUP_RETENTION_POLICY: str
        TENANT_POSTGRES_BACKUP_RUSTFS_CA_SECRET_NAME: str
        TENANT_POSTGRES_BACKUP_RUSTFS_ENDPOINT: NotRequired[str]
        TENANT_POSTGRES_BACKUP_RUSTFS_SECRET_NAME: str
        TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAME: NotRequired[str]
        TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAMESPACE: NotRequired[str]
        TENANT_POSTGRES_BACKUP_SCHEDULE: str
        TENANT_POSTGRES_BACKUP_SERVICE_ACCOUNT_NAME: str
        TENANT_POSTGRES_BACKUPS_ENABLED: bool
        VAULT_CERT: NotRequired[tuple[str, str]]
        VAULT_DB_CREDS_PATH: NotRequired[str]
        VAULT_PREFIX: NotRequired[str]
        VAULT_SIGNING_KEY: NotRequired[str]
        VAULT_SIGNING_MOUNT: NotRequired[str]
        VAULT_TOKEN_FILE: NotRequired[str]
        VAULT_TOKEN_UNWRAP: NotRequired[bool]
        VAULT_TOKEN: NotRequired[str]
        VAULT_URL: str
        VAULT_VERIFY: bool
        WRITE_BACKENDS: bool
        WTF_CSRF_ENABLED: bool
