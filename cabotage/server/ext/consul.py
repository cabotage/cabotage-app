import os

import consul

from cabotage.utils.context import modified_environ
from flask import g


class Consul(object):
    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        self.consul_host = app.config.get("CONSUL_HOST", "127.0.0.1")
        self.consul_port = app.config.get("CONSUL_PORT", "8500")
        self.consul_scheme = app.config.get("CONSUL_SCHEME", "http")
        self.consul_verify = app.config.get("CONSUL_VERIFY", False)
        self.consul_cert = app.config.get("CONSUL_CERT", None)
        self.consul_prefix = app.config.get("CONSUL_PREFIX", "cabotage")
        self.consul_token_file = app.config.get(
            "CONSUL_TOKEN_FILE", os.path.expanduser("~/.consul-token")
        )
        self.consul_token = app.config.get("CONSUL_TOKEN", None)

        if self.consul_token is None:
            if os.path.exists(self.consul_token_file):
                with open(self.consul_token_file, "r") as consul_token_file:
                    self.consul_token = consul_token_file.read().lstrip().rstrip()

        app.teardown_appcontext(self.teardown)

    def connect_consul(self):
        # Ignore default environment variables
        with modified_environ(
            "CONSUL_HTTP_ADDR", "CONSUL_HTTP_SSL", "CONSUL_HTTP_SSL_VERIFY"
        ):
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
        g.pop("consul_client", None)

    @property
    def consul_connection(self):
        if "consul_client" not in g:
            g.consul_client = self.connect_consul()
        return g.consul_client
