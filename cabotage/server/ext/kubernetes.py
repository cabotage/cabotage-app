from __future__ import annotations

from typing import TYPE_CHECKING

from flask import g

from kubernetes.client.api_client import ApiClient
from kubernetes.config import load_incluster_config, load_kube_config

if TYPE_CHECKING:
    from cabotage._types.server import TypedFlask


class Kubernetes(object):
    def __init__(self, app: TypedFlask | None = None) -> None:
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app: TypedFlask) -> None:
        try:
            load_incluster_config()
        except Exception:
            try:
                load_kube_config(context=app.config["KUBERNETES_CONTEXT"])
            except Exception:
                if app.config["KUBERNETES_ENABLED"]:
                    raise

        app.teardown_appcontext(self.teardown)

    def connect_kubernetes(self) -> ApiClient:
        kubernetes_client = ApiClient()
        return kubernetes_client

    def teardown(self, exception: BaseException | None) -> None:
        g.pop("kubernetes_client", None)

    @property
    def kubernetes_client(self) -> ApiClient:
        if "kubernetes_client" not in g:
            g.kubernetes_client = self.connect_kubernetes()
        return g.kubernetes_client
