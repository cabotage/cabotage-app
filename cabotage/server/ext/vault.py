import os

from base64 import(
    b64decode,
    b64encode,
)

import hvac

from flask import current_app
from flask import _app_ctx_stack as stack

from cabotage.utils.cert_hacks import construct_cert_from_public_key


class Vault(object):

    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        self.vault_url = app.config.get('VAULT_URL', 'http://127.0.0.1:8200')
        self.vault_verify = app.config.get('VAULT_VERIFY', False)
        self.vault_cert = app.config.get('VAULT_CERT', None)
        self.vault_token = app.config.get('VAULT_TOKEN', None)
        self.vault_token_file = app.config.get('VAULT_TOKEN_FILE', os.path.expanduser('~/.vault-token'))
        self.vault_token_unwrap = app.config.get('VAULT_TOKEN_UNWRAP', False)
        self.vault_prefix = app.config.get('VAULT_PREFIX', 'secret/cabotage')
        self.vault_signing_mount = app.config.get('VAULT_SIGNING_MOUNT', 'transit')
        self.vault_signing_key = app.config.get('VAULT_SIGNING_KEY', 'cabotage-app')

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

    @property
    def signing_public_key(self):
        VAULT_TRANSIT_KEY = f'{self.vault_signing_mount}/keys/{self.vault_signing_key}'
        key_data = self.vault_connection.read(VAULT_TRANSIT_KEY)
        keys = key_data['data']['keys']
        latest = str(key_data['data']['latest_version'])
        return keys[latest]['public_key'].encode()

    @property
    def signing_cert(self):
        return construct_cert_from_public_key(
            self.sign_payload,
            self.signing_public_key,
            'cabotage-app',
        )

    def sign_payload(self, payload, algorithm='sha2-256'):
        if algorithm not in ('sha2-224', 'sha2-256', 'sha2-384', 'sha2-512'):
            raise KeyError(f'Specified algorithm ({algorithm}) not supported!')
        VAULT_TRANSIT_SIGNING = f'{self.vault_signing_mount}/sign/{self.vault_signing_key}/{algorithm}'
        signature_response = self.vault_connection.write(
            VAULT_TRANSIT_SIGNING,
            input=b64encode(payload.encode()).decode(),
        )
        signature_encoded = signature_response['data']['signature'].split(':')[2]
        return b64decode(signature_encoded)
