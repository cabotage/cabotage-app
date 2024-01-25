import datetime

import kubernetes

from celery import shared_task

from cabotage.server import kubernetes as kubernetes_ext


@shared_task()
def reap_pods():
    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)
    pods = core_api_instance.list_pod_for_all_namespaces(
        label_selector="resident-pod.cabotage.io=true",
    )
    candidate = sorted(pods.items, key=lambda pod: pod.status.start_time)[0]
    lookback = datetime.datetime.now().replace(
        tzinfo=datetime.timezone.utc
    ) - datetime.timedelta(days=7)
    if candidate.status.start_time < lookback:
        core_api_instance.delete_namespaced_pod(
            candidate.metadata.name, candidate.metadata.namespace
        )
