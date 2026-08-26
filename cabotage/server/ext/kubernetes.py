from __future__ import annotations
from typing import TYPE_CHECKING, cast

from flask import g

from kubernetes.client.api_client import ApiClient
from kubernetes.config import load_incluster_config, load_kube_config

if TYPE_CHECKING:
    from flask import Flask


class Kubernetes(object):
    def __init__(self, app: Flask | None = None) -> None:
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask):
        try:
            load_incluster_config()
        except Exception:
            try:
                # FIXME: Remove once "typed config" is implemented
                load_kube_config(context=cast(str, app.config["KUBERNETES_CONTEXT"]))
            except Exception:
                if app.config["KUBERNETES_ENABLED"]:
                    raise

        _ = app.teardown_appcontext(self.teardown)

    def connect_kubernetes(self):
        kubernetes_client = ApiClient()
        return kubernetes_client

    def teardown(self, exception):
        g.pop("kubernetes_client", None)

    @property
    def kubernetes_client(self):
        if "kubernetes_client" not in g:
            g.kubernetes_client = self.connect_kubernetes()
        return g.kubernetes_client
