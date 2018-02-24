import os

from flask import current_app
from flask import _app_ctx_stack as stack

import kubernetes


class Kubernetes(object):

    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        try:
            kubernetes.config.load_incluster_config()
        except Exception:
            try:
                kubernetes.config.load_kube_config()
            except Exception:
                raise

        self.configuration = kubernetes.client.Configuration()

        app.teardown_appcontext(self.teardown)

    def connect_kubernetes(self):
        kubernetes_client = kubernetes.client.ApiClient(self.configuration)
        return kubernetes_client

    def teardown(self, exception):
        ctx = stack.top
        if hasattr(ctx, 'kubernetes_client'):
            del(ctx.kubernetes_client)

    @property
    def kubernetes_client(self):
        ctx = stack.top
        if ctx is not None:
            if not hasattr(ctx, 'kubernetes_client'):
                ctx.kubernetes_client = self.connect_kubernetes()
            return ctx.kubernetes_client
