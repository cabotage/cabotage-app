import os

from flask_env import MetaFlaskEnv

basedir = os.path.abspath(os.path.dirname(__file__))


class Config(metaclass=MetaFlaskEnv):
    ENV_PREFIX = "CABOTAGE_"

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
    WRITE_BACKENDS = True
    CONSUL_HOST = 'consul'
    CONSUL_PORT = 8500
    CONSUL_SCHEME = 'http'
    CONSUL_VERIFY = False
    CONSUL_CERT = None
    CONSUL_PREFIX = 'cabotage'
    VAULT_TOKEN = None
    VAULT_TOKEN_UNWRAP = False
    VAULT_URL = 'http://vault:8200'
    VAULT_VERIFY = False
    VAULT_CERT = None
    VAULT_PREFIX = 'secret/cabotage'
    VAULT_SIGNING_MOUNT = 'transit'
    VAULT_SIGNING_KEY = 'cabotage-app'
    REGISTRY = 'registry:5000'
    REGISTRY_SECURE = False
    REGISTRY_AUTH_SECRET = 'v3rys3cur3'
    DOCKER_URL = 'tcp://cabotage-dind:2375'
    DOCKER_SECURE = False
    DOCKER_VERIFY = None
    MINIO_ENDPOINT = 'minio:9000'
    MINIO_BUCKET = 'cabotage-registry'
    MINIO_ACCESS_KEY = 'MINIOACCESSKEY'
    MINIO_SECRET_KEY = 'MINIOSECRETKEY'
    MINIO_SECURE = False
    MINIO_PREFIX = 'cabotage-builds'
    CELERY_BROKER_URL='redis://redis:6379',
    CELERY_RESULT_BACKEND='redis://redis:6379'
