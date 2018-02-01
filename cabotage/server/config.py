import os
basedir = os.path.abspath(os.path.dirname(__file__))


class BaseConfig(object):
    """Base configuration."""
    SECRET_KEY = 'my_precious'
    DEBUG = False
    MAX_CONTENT_LENGTH = 32 * 1024 * 1024
    BCRYPT_LOG_ROUNDS = 13
    WTF_CSRF_ENABLED = True
    DEBUG_TB_ENABLED = False
    DEBUG_TB_INTERCEPT_REDIRECTS = False
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECURITY_PASSWORD_SALT = 'my_precious'
    SECURITY_TRACKABLE = True
    SECURITY_CHANGEABLE = True
    SECURITY_CONFIRMABLE = True
    SECURITY_REGISTERABLE = True
    SECURITY_RECOVERABLE = True
    SECURITY_EMAIL_SENDER = 'noreply@localhost'
    SECURITY_USER_IDENTITY_ATTRIBUTES = ['email', 'username']
    SECURITY_POST_REGISTER_VIEW = 'security.login'
    MAIL_SERVER = 'debugmail.io'
    MAIL_PORT = 25
    MAIL_USE_TLS = False
    MAIL_USE_SSL = False
    MAIL_USERNAME = 'ewdurbin@gmail.com'
    MAIL_PASSWORD = '40d5a170-f648-11e7-8c0d-e34c4746c3e2'
    MAIL_DEFAULT_SENDER = 'noreply@localhost'
    BOOTSTRAP_SERVE_LOCAL = True
    HUMANIZE_USE_UTC = True
    CABOTAGE_WRITE_BACKENDS = True
    CABOTAGE_CONSUL_HOST = 'consul'
    CABOTAGE_CONSUL_PORT = 8500
    CABOTAGE_CONSUL_SCHEME = 'http'
    CABOTAGE_CONSUL_VERIFY = False
    CABOTAGE_CONSUL_CERT = None
    CABOTAGE_CONSUL_PREFIX = 'cabotage'
    CABOTAGE_VAULT_TOKEN = 'deadbeef-dead-beef-dead-beefdeadbeef'
    CABOTAGE_VAULT_TOKEN_UNWRAP = False
    CABOTAGE_VAULT_URL = 'http://vault:8200'
    CABOTAGE_VAULT_VERIFY = False
    CABOTAGE_VAULT_CERT = None
    CABOTAGE_VAULT_PREFIX = 'secret/cabotage'
    CABOTAGE_VAULT_SIGNING_MOUNT = 'transit'
    CABOTAGE_VAULT_SIGNING_KEY = 'cabotage-app'
    CABOTAGE_REGISTRY = 'registry:5000'
    CABOTAGE_REGISTRY_SECURE = False
    CABOTAGE_REGISTRY_AUTH_SECRET = 'v3rys3cur3'
    CABOTAGE_MINIO_ENDPOINT = 'minio:9000'
    CABOTAGE_MINIO_BUCKET = 'cabotage-registry'
    CABOTAGE_MINIO_ACCESS_KEY = 'MINIOACCESSKEY'
    CABOTAGE_MINIO_SECRET_KEY = 'MINIOSECRETKEY'
    CABOTAGE_MINIO_SECURE = False
    CABOTAGE_MINIO_PREFIX = 'cabotage-builds'
    CELERY_BROKER_URL='redis://redis:6379',
    CELERY_RESULT_BACKEND='redis://redis:6379'

class DevelopmentConfig(BaseConfig):
    """Development configuration."""
    DEBUG = True
    BCRYPT_LOG_ROUNDS = 4
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = 'postgresql://postgres@db/cabotage_dev'
    DEBUG_TB_ENABLED = True
    SECURITY_CONFIRMABLE = False


class TestingConfig(BaseConfig):
    """Testing configuration."""
    DEBUG = True
    DEBUG_TB_ENABLED = False
    TESTING = True
    BCRYPT_LOG_ROUNDS = 4
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = 'postgresql://localhost/cabotage_test'
    DEBUG_TB_ENABLED = False
    PRESERVE_CONTEXT_ON_EXCEPTION = False


class ProductionConfig(BaseConfig):
    """Production configuration."""
    SECRET_KEY = 'my_precious'
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = 'postgresql://localhost/example'
    DEBUG_TB_ENABLED = False
