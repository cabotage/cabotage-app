import os

import consul
import hvac

from flask import current_app
from flask import _app_ctx_stack as stack


class ConfigWriter(object):

    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        self.consul_host = app.config.get('CABOTAGE_CONSUL_HOST', '127.0.0.1')
        self.consul_port = app.config.get('CABOTAGE_CONSUL_PORT', '8500')
        self.consul_scheme = app.config.get('CABOTAGE_CONSUL_SCHEME', 'http')
        self.consul_verify = app.config.get('CABOTAGE_CONSUL_VERIFY', False)
        self.consul_cert = app.config.get('CABOTAGE_CONSUL_CERT', None)
        self.vault_url = app.config.get('CABOTAGE_VAULT_URL', 'http://127.0.0.1:8200')
        self.vault_verify = app.config.get('CABOTAGE_VAULT_VERIFY', False)
        self.vault_cert = app.config.get('CABOTAGE_VAULT_CERT', None)
        self.vault_token = app.config.get('CABOTAGE_VAULT_TOKEN', None)
        self.vault_token_file = app.config.get('CABOTAGE_VAULT_TOKEN_FILE', os.path.expanduser('~/.vault-token'))
        self.vault_token_unwrap = app.config.get('CABOTAGE_VAULT_TOKEN_UNWRAP', False)

        if self.vault_token is None:
            if os.path.exists(self.vault_token_file):
                with open(self.vault_token_file, 'rU') as vault_token_file:
                    self.vault_token = vault_token_file.read().lstrip().rstrip()

        # Unwrap!
        #if self.vault_token_unwrap:
        #    unwrap_dang_token

        app.teardown_appcontext(self.teardown)

    def connect_consul(self):
        consul_client = consul.Consul(
            host=self.consul_host,
            port=self.consul_port,
            scheme=self.consul_scheme,
            verify=self.consul_verify,
            cert=self.consul_cert,
        )
        return consul_client

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
        if hasattr(ctx, 'consul_client'):
            del(ctx.consul_client)
        if hasattr(ctx, 'vault_client'):
            del(ctx.vault_client)

    @property
    def consul_connection(self):
        ctx = stack.top
        if ctx is not None:
            if not hasattr(ctx, 'consul_client'):
                ctx.consul_client = self.connect_consul()
            return ctx.consul_client

    @property
    def vault_connection(self):
        ctx = stack.top
        if ctx is not None:
            if not hasattr(ctx, 'vault_client'):
                ctx.vault_client = self.connect_vault()
            return ctx.vault_client

    def write_configuration(self, org_slug, project_slug, app_slug, configuration):
        version = configuration.version_id + 1 if configuration.version_id else 1
        if configuration.secret:
            key_name = (f'cabotage-secrets/automation/{org_slug}/'
                        f'{project_slug}_{app_slug}/configuration/'
                        f'{configuration.name}/{version}')
            storage = 'vault'
            self.vault_connection.write(
                key_name, **{configuration.name: configuration.value},
            )
        else:
            key_name = (f'cabotage/automation/{org_slug}/'
                        f'{project_slug}_{app_slug}/configuration/'
                        f'{configuration.name}/{version}/{configuration.name}')
            storage = 'consul'
            self.consul_connection.kv.put(key_name, configuration.value)
            key_name = '/'.join(key_name.split('/')[:-1])
        return f'{storage}:{key_name}'
