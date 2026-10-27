import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import kubernetes.client
import pytest
from kubernetes.client.exceptions import ApiException

import cabotage.celery.tasks.deploy as deploy_module
from cabotage.server.wsgi import app as _app

DEPLOYMENT_ID = uuid.UUID("4ec1f9e5-fbf0-4b9a-9ffe-8ea1284a5be2")
WORKER_ARGS = ["envconsul", "-config=/etc/cabotage/envconsul-worker.hcl"]


@dataclass
class Cluster:
    apps: MagicMock
    batch: MagicMock
    deploy_release: MagicMock
    deployment: SimpleNamespace
    refresh_heartbeat: MagicMock


@pytest.fixture
def cluster(tmp_path: Path) -> Iterator[Cluster]:
    namespace_file = tmp_path / "namespace"
    _ = namespace_file.write_text("cabotage\n")
    deployment = SimpleNamespace(id=DEPLOYMENT_ID, complete=False, error=False)
    core = MagicMock()
    core.read_namespaced_pod.return_value = kubernetes.client.V1Pod(
        metadata=kubernetes.client.V1ObjectMeta(
            owner_references=[
                kubernetes.client.V1OwnerReference(
                    api_version="apps/v1",
                    kind="ReplicaSet",
                    name="cabotage-app-worker-7ccccd8887",
                    uid="rs",
                )
            ],
        )
    )
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = kubernetes.client.V1Deployment(
        spec=kubernetes.client.V1DeploymentSpec(
            selector=kubernetes.client.V1LabelSelector(),
            template=kubernetes.client.V1PodTemplateSpec(
                metadata=kubernetes.client.V1ObjectMeta(
                    labels={"app": "cabotage-app", "process": "worker"}
                ),
                spec=kubernetes.client.V1PodSpec(
                    containers=[
                        kubernetes.client.V1Container(
                            name="worker",
                            args=list(WORKER_ARGS),
                            liveness_probe=kubernetes.client.V1Probe(),
                        )
                    ]
                ),
            ),
        )
    )
    batch = MagicMock()
    with (
        _app.app_context(),
        patch.object(deploy_module, "_SERVICE_ACCOUNT_NAMESPACE", namespace_file),
        patch.object(deploy_module, "kubernetes_ext") as kubernetes_ext,
        patch.object(deploy_module, "Deployment") as model,
        patch.object(deploy_module, "get_redis_client"),
        patch.object(deploy_module, "refresh_heartbeat") as refresh_heartbeat,
        patch.object(deploy_module, "deploy_release") as deploy_release,
        patch("kubernetes.client.CoreV1Api", return_value=core),
        patch("kubernetes.client.AppsV1Api", return_value=apps),
        patch("kubernetes.client.BatchV1Api", return_value=batch),
    ):
        kubernetes_ext.in_cluster = True
        model.query.filter_by.return_value.first.return_value = deployment
        yield Cluster(
            apps=apps,
            batch=batch,
            deploy_release=deploy_release,
            deployment=deployment,
            refresh_heartbeat=refresh_heartbeat,
        )


def test_in_cluster_deploy_runs_in_a_job_from_the_worker_template(
    cluster: Cluster,
) -> None:
    deploy_module.run_deploy(deployment_id=DEPLOYMENT_ID)

    cluster.deploy_release.assert_not_called()
    cluster.apps.read_namespaced_deployment.assert_called_once_with(
        "cabotage-app-worker", "cabotage"
    )
    namespace, job = cluster.batch.create_namespaced_job.call_args.args
    assert namespace == "cabotage"
    assert isinstance(job, kubernetes.client.V1Job)
    assert job.metadata is not None and job.spec is not None
    assert job.metadata.name == f"deploy-{DEPLOYMENT_ID}"
    assert job.spec.backoff_limit == 0
    pod = job.spec.template
    assert pod.metadata is not None and pod.spec is not None
    assert pod.metadata.labels == {"app": "cabotage-app"}
    assert pod.spec.restart_policy == "Never"
    container = pod.spec.containers[0]
    assert container.args == [
        *WORKER_ARGS,
        "-once",
        "python",
        "-m",
        "cabotage.scripts.deploy",
        str(DEPLOYMENT_ID),
    ]
    assert container.liveness_probe is None


def test_worker_without_envconsul_runs_the_deploy_as_the_command(
    cluster: Cluster,
) -> None:
    worker = cluster.apps.read_namespaced_deployment.return_value
    worker_container = worker.spec.template.spec.containers[0]
    worker_container.command = ["celery"]
    worker_container.args = ["-A", "cabotage.celery.worker.celery_app", "worker"]

    deploy_module.run_deploy(deployment_id=DEPLOYMENT_ID)

    job = cluster.batch.create_namespaced_job.call_args.args[1]
    container = job.spec.template.spec.containers[0]
    assert container.command == [
        "python",
        "-m",
        "cabotage.scripts.deploy",
        str(DEPLOYMENT_ID),
    ]
    assert container.args is None


def test_redelivered_task_keeps_the_existing_job(cluster: Cluster) -> None:
    cluster.batch.create_namespaced_job.side_effect = ApiException(status=409)

    deploy_module.run_deploy(deployment_id=DEPLOYMENT_ID)


def test_job_submission_errors_are_raised(cluster: Cluster) -> None:
    cluster.batch.create_namespaced_job.side_effect = ApiException(status=403)

    with pytest.raises(ApiException):
        deploy_module.run_deploy(deployment_id=DEPLOYMENT_ID)


def test_outside_the_cluster_deploys_inline(cluster: Cluster) -> None:
    with patch.object(deploy_module.kubernetes_ext, "in_cluster", False):
        deploy_module.run_deploy(deployment_id=DEPLOYMENT_ID)

    cluster.deploy_release.assert_called_once()
    cluster.batch.create_namespaced_job.assert_not_called()


def test_job_is_submitted_when_the_heartbeat_fails(cluster: Cluster) -> None:
    cluster.refresh_heartbeat.side_effect = ConnectionError("redis down")

    deploy_module.run_deploy(deployment_id=DEPLOYMENT_ID)

    cluster.batch.create_namespaced_job.assert_called_once()


@pytest.mark.parametrize("state", ["complete", "error"])
def test_finished_deployments_are_not_applied(cluster: Cluster, state: str) -> None:
    setattr(cluster.deployment, state, True)

    deploy_module.execute_deployment(DEPLOYMENT_ID)

    cluster.deploy_release.assert_not_called()
