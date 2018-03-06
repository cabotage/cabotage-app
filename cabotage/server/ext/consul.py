import os

import consul

from flask import current_app
from flask import _app_ctx_stack as stack


class Consul(object):

    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        self.consul_host = app.config.get('CONSUL_HOST', '127.0.0.1')
        self.consul_port = app.config.get('CONSUL_PORT', '8500')
        self.consul_scheme = app.config.get('CONSUL_SCHEME', 'http')
        self.consul_verify = app.config.get('CONSUL_VERIFY', False)
        self.consul_cert = app.config.get('CONSUL_CERT', None)
        self.consul_prefix = app.config.get('CONSUL_PREFIX', 'cabotage')
        self.consul_token = app.config.get('CONSUL_TOKEN', None)

        app.teardown_appcontext(self.teardown)

    def connect_consul(self):
        consul_client = consul.Consul(
            host=self.consul_host,
            port=self.consul_port,
            scheme=self.consul_scheme,
            verify=self.consul_verify,
            cert=self.consul_cert,
            token=self.consul_token,
        )
        return consul_client

    def teardown(self, exception):
        ctx = stack.top
        if hasattr(ctx, 'consul_client'):
            del(ctx.consul_client)

    @property
    def consul_connection(self):
        ctx = stack.top
        if ctx is not None:
            if not hasattr(ctx, 'consul_client'):
                ctx.consul_client = self.connect_consul()
            return ctx.consul_client
