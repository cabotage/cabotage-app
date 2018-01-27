import os

import hvac

from flask import current_app
from flask import _app_ctx_stack as stack


class Vault(object):

    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        self.vault_url = app.config.get('CABOTAGE_VAULT_URL', 'http://127.0.0.1:8200')
        self.vault_verify = app.config.get('CABOTAGE_VAULT_VERIFY', False)
        self.vault_cert = app.config.get('CABOTAGE_VAULT_CERT', None)
        self.vault_token = app.config.get('CABOTAGE_VAULT_TOKEN', None)
        self.vault_token_file = app.config.get('CABOTAGE_VAULT_TOKEN_FILE', os.path.expanduser('~/.vault-token'))
        self.vault_token_unwrap = app.config.get('CABOTAGE_VAULT_TOKEN_UNWRAP', False)
        self.vault_prefix = app.config.get('CABOTAGE_VAULT_PREFIX', 'secret/cabotage')
        self.vault_signing_mount = app.config.get('CABOTAGE_VAULT_SIGNING_MOUNT', 'transit')
        self.vault_signing_key = app.config.get('CABOTAGE_VAULT_SIGNING_KEY', 'cabotage-app')

        if self.vault_token is None:
            if os.path.exists(self.vault_token_file):
                with open(self.vault_token_file, 'rU') as vault_token_file:
                    self.vault_token = vault_token_file.read().lstrip().rstrip()

        # Unwrap!
        #if self.vault_token_unwrap:
        #    unwrap_dang_token

        app.teardown_appcontext(self.teardown)

    def connect_vault(self):
        vault_client = hvac.Client(
            url=self.vault_url,
            token=self.vault_token,
            verify=self.vault_verify,
            cert=self.vault_cert,
        )
        return vault_client

    def teardown(self, exception):
        ctx = stack.top
        if hasattr(ctx, 'vault_client'):
            del(ctx.vault_client)

    @property
    def vault_connection(self):
        ctx = stack.top
        if ctx is not None:
            if not hasattr(ctx, 'vault_client'):
                ctx.vault_client = self.connect_vault()
            return ctx.vault_client
