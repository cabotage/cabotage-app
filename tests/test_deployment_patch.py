"""Tests for create_deployment against the real kubernetes client.

These go through the actual client stack to a stub HTTP server rather than
mocking AppsV1Api, so a kubernetes package bump that changes client signatures
fails here instead of in production deploys.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import kubernetes.client
import pytest

import cabotage.celery.tasks.deploy as deploy_module

_DEPLOY_MODULE = "cabotage.celery.tasks.deploy"
_DEPLOYMENTS_PATH = "/apis/apps/v1/namespaces/test-ns/deployments"


def _deployment_json(name):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": "test-ns"},
        "spec": {
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"containers": [{"name": "web", "image": "img:1"}]},
            },
        },
    }


@pytest.fixture
def k8s_server():
    """Stub API server; ``existing`` controls whether the GET finds the Deployment."""
    state = {"existing": True, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, payload):
            out = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def _record(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            state["requests"].append(
                {
                    "method": self.command,
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type"),
                    "body": body,
                }
            )
            return body

        def do_GET(self):
            self._record()
            if state["existing"]:
                self._reply(200, _deployment_json(self.path.rsplit("/", 1)[-1]))
            else:
                self._reply(404, {"kind": "Status", "code": 404})

        def do_PATCH(self):
            body = self._record()
            self._reply(200, body)

        def do_POST(self):
            body = self._record()
            self._reply(201, body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = kubernetes.client.Configuration()
    config.host = f"http://127.0.0.1:{server.server_port}"
    state["api"] = kubernetes.client.AppsV1Api(kubernetes.client.ApiClient(config))
    yield state
    server.shutdown()
    server.server_close()


def _rendered_deployment():
    return kubernetes.client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=kubernetes.client.V1ObjectMeta(name="proj-app-web"),
        spec=kubernetes.client.V1DeploymentSpec(
            selector=kubernetes.client.V1LabelSelector(
                match_labels={"app": "proj-app-web"}
            ),
            template=kubernetes.client.V1PodTemplateSpec(
                metadata=kubernetes.client.V1ObjectMeta(labels={"app": "proj-app-web"}),
                spec=kubernetes.client.V1PodSpec(
                    containers=[
                        kubernetes.client.V1Container(name="web", image="img:2")
                    ]
                ),
            ),
        ),
    )


def _create_deployment(api):
    with patch(
        f"{_DEPLOY_MODULE}.render_deployment", return_value=_rendered_deployment()
    ):
        return deploy_module.create_deployment(
            api, "test-ns", None, "sa-name", "web", "deploy-id"
        )


class TestCreateDeployment:
    def test_existing_deployment_is_merge_patched(self, k8s_server):
        result = _create_deployment(k8s_server["api"])

        patches = [r for r in k8s_server["requests"] if r["method"] == "PATCH"]
        assert len(patches) == 1
        assert patches[0]["path"] == f"{_DEPLOYMENTS_PATH}/proj-app-web"
        # Strategic merge patch would keep containers dropped from the spec.
        assert patches[0]["content_type"] == "application/merge-patch+json"
        assert patches[0]["body"]["spec"]["template"]["spec"]["containers"] == [
            {"name": "web", "image": "img:2"}
        ]
        assert isinstance(result, kubernetes.client.V1Deployment)
        assert result.spec.template.spec.containers[0].image == "img:2"

    def test_missing_deployment_is_created(self, k8s_server):
        k8s_server["existing"] = False

        result = _create_deployment(k8s_server["api"])

        methods = [r["method"] for r in k8s_server["requests"]]
        assert methods == ["GET", "POST"]
        assert k8s_server["requests"][1]["path"] == _DEPLOYMENTS_PATH
        assert isinstance(result, kubernetes.client.V1Deployment)
