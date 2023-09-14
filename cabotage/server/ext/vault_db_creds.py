import atexit
import hashlib
import os

from urllib.parse import urlsplit, urlunsplit

import hvac

from flask import current_app
from flask import _app_ctx_stack as stack


class VaultDBCreds(object):
    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        if app.config.get("SQLALCHEMY_DATABASE_URI", None):
            return
        if app.config.get("VAULT_DB_CREDS_PATH", None):
            self.vault_url = app.config.get("VAULT_URL", "http://127.0.0.1:8200")
            self.vault_verify = app.config.get("VAULT_VERIFY", False)
            self.vault_cert = app.config.get("VAULT_CERT", None)
            self.vault_token = app.config.get("VAULT_TOKEN", None)
            self.vault_token_file = app.config.get(
                "VAULT_TOKEN_FILE", os.path.expanduser("~/.vault-token")
            )
            self.vault_token_unwrap = app.config.get("VAULT_TOKEN_UNWRAP", False)
            self.vault_db_database_uri = app.config.get("VAULT_DB_DATABASE_URI", None)
            self.vault_db_creds_path = app.config.get(
                "VAULT_DB_CREDS_PATH", "database/creds/cabotage"
            )
            self.vault_lease_path = app.config.get("VAULT_LEASE_PATH", "")

            self.rendered_uri = None
            self.vault_lease_id = None
            self.vault_lease_duration = -1

            if self.vault_db_database_uri is None:
                raise RuntimeError(
                    "Unable to configure a database uri, "
                    "VAULT_DB_DATABASE_URI is required when "
                    "VAULT_DB_CREDS_PATH is specified"
                )

            if self.vault_token is None:
                if os.path.exists(self.vault_token_file):
                    with open(self.vault_token_file, "r") as vault_token_file:
                        self.vault_token = vault_token_file.read().lstrip().rstrip()

            self.logger = app.logger
            with app.app_context():
                self.fetch_database_credentials()
            atexit.register(self.revoke_credentials)
        else:
            raise RuntimeError(
                "Unable to configure a database uri, "
                "one of SQLALCHEMY_DATABASE_URI or "
                "VAULT_DB_CREDS_PATH must  be specified"
            )

        app.teardown_appcontext(self.teardown)

    def revoke_credentials(self):
        self.logger.info(f"revoking {self.vault_lease_id} at shutdown")
        self.connect_vault().write("sys/leases/revoke", lease_id=self.vault_lease_id)
        self.logger.info(f"revoked {self.vault_lease_id}")

    def fetch_database_credentials(self):
        response = self.vault_connection.read(self.vault_db_creds_path)
        parsed_uri = urlsplit(self.vault_db_database_uri)
        new_netloc = (
            f"{response['data']['username']}:{response['data']['password']}"
            f"@{parsed_uri.hostname}"
        )
        constructed = urlunsplit(parsed_uri._replace(netloc=new_netloc))
        self.rendered_uri = constructed
        self.vault_lease_id = response["lease_id"]
        self.vault_lease_duration = response["lease_duration"]
        if os.path.exists(os.path.join(self.vault_lease_path, "leases")):
            lease_sha = hashlib.sha256(self.vault_lease_id.encode("utf-8")).hexdigest()
            with open(
                os.path.join(self.vault_lease_path, "leases", lease_sha), "wb"
            ) as lease_file:
                lease_file.write(self.vault_lease_id.encode("utf-8"))
            self.logger.info(
                "wrote lease file to "
                f'{os.path.join(self.vault_lease_path, "leases", lease_sha)}'
            )
        else:
            self.logger.warning(
                "no lease file written for "
                f"{self.vault_lease_id}! someone needs to renew it!"
            )
        current_app.config["SQLALCHEMY_DATABASE_URI"] = constructed

    def connect_vault(self):
        vault_db_creds_client = hvac.Client(
            url=self.vault_url,
            token=self.vault_token,
            verify=self.vault_verify,
            cert=self.vault_cert,
        )
        return vault_db_creds_client

    def teardown(self, exception):
        ctx = stack.top
        if hasattr(ctx, "vault_db_creds_client"):
            del ctx.vault_client

    @property
    def vault_connection(self):
        ctx = stack.top
        if ctx is not None:
            if not hasattr(ctx, "vault_db_creds_client"):
                ctx.vault_client = self.connect_vault()
            return ctx.vault_client
