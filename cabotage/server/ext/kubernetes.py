from flask import g

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
                kubernetes.config.load_kube_config(
                    context=app.config["KUBERNETES_CONTEXT"]
                )
            except Exception:
                if app.config["KUBERNETES_ENABLED"]:
                    raise

        app.teardown_appcontext(self.teardown)

    def connect_kubernetes(self):
        kubernetes_client = kubernetes.client.ApiClient()
        return kubernetes_client

    def teardown(self, exception):
        g.pop("kubernetes_client", None)

    @property
    def kubernetes_client(self):
        if "kubernetes_client" not in g:
            g.kubernetes_client = self.connect_kubernetes()
        return g.kubernetes_client
