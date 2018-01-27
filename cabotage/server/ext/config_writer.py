import os

from flask import current_app
from flask import _app_ctx_stack as stack


class ConfigWriter(object):

    def __init__(self, app=None, consul=None, vault=None):
        self.app = app
        self.consul = consul
        self.vault = vault
        if app is not None:
            self.init_app(app, consul, vault)

    def init_app(self, app, consul, vault):
        self.consul = consul
        self.vault = vault
        self.consul_prefix = app.config.get('CABOTAGE_CONSUL_PREFIX', 'cabotage')
        self.vault_prefix = app.config.get('CABOTAGE_VAULT_PREFIX', 'secret/cabotage')

        app.teardown_appcontext(self.teardown)

    def teardown(self, exception):
        pass

    def write_configuration(self, org_slug, project_slug, app_slug, configuration):
        version = configuration.version_id + 1 if configuration.version_id else 1
        if configuration.secret:
            if self.vault is None:
                raise RuntimeError('No Vault extension configured!')
            key_name = (f'{self.vault_prefix}/automation/{org_slug}/'
                        f'{project_slug}_{app_slug}/configuration/'
                        f'{configuration.name}/{version}')
            storage = 'vault'
            self.vault.vault_connection.write(
                key_name, **{configuration.name: configuration.value},
            )
        else:
            if self.consul is None:
                raise RuntimeError('No Consul extension configured!')
            key_name = (f'{self.consul_prefix}/automation/{org_slug}/'
                        f'{project_slug}_{app_slug}/configuration/'
                        f'{configuration.name}/{version}/{configuration.name}')
            storage = 'consul'
            self.consul.consul_connection.kv.put(key_name, configuration.value)
            key_name = '/'.join(key_name.split('/')[:-1])
        return f'{storage}:{key_name}'
