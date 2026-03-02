import datetime

import kubernetes

from celery import shared_task
from flask import current_app

from cabotage.server import db, github_app, kubernetes as kubernetes_ext
from cabotage.server.models.projects import Deployment, Image, Release
from cabotage.utils.build_log_stream import get_redis_client, heartbeat_key
from cabotage.utils.github import post_deployment_status_update


@shared_task()
def reap_stale_builds():
    """Find stuck image builds, release builds, and deploys with no heartbeat."""
    redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(seconds=90)

    # Images: built=False, error=False, updated < cutoff, no heartbeat
    stuck_images = Image.query.filter(
        Image.built == False,  # noqa: E712
        Image.error == False,  # noqa: E712
        Image.updated < cutoff,
    ).all()
    for image in stuck_images:
        key = heartbeat_key("image_build", str(image.id))
        if not redis_client.exists(key):
            image.error = True
            image.error_detail = "Reaped: build timed out with no progress"
            if (
                image.image_metadata
                and "installation_id" in image.image_metadata
                and "statuses_url" in image.image_metadata
            ):
                access_token = github_app.fetch_installation_access_token(
                    image.image_metadata["installation_id"]
                )
                post_deployment_status_update(
                    access_token,
                    image.image_metadata["statuses_url"],
                    "failure",
                    "Image build timed out.",
                )

    # Releases: built=False, error=False, updated < cutoff, no heartbeat
    stuck_releases = Release.query.filter(
        Release.built == False,  # noqa: E712
        Release.error == False,  # noqa: E712
        Release.updated < cutoff,
    ).all()
    for release in stuck_releases:
        key = heartbeat_key("release_build", str(release.id))
        if not redis_client.exists(key):
            release.error = True
            release.error_detail = "Reaped: release build timed out with no progress"
            if (
                release.release_metadata
                and "installation_id" in release.release_metadata
                and "statuses_url" in release.release_metadata
            ):
                access_token = github_app.fetch_installation_access_token(
                    release.release_metadata["installation_id"]
                )
                post_deployment_status_update(
                    access_token,
                    release.release_metadata["statuses_url"],
                    "failure",
                    "Release build timed out.",
                )

    # Deployments: complete=False, error=False, updated < cutoff, no heartbeat
    stuck_deployments = Deployment.query.filter(
        Deployment.complete == False,  # noqa: E712
        Deployment.error == False,  # noqa: E712
        Deployment.updated < cutoff,
    ).all()
    for deployment in stuck_deployments:
        key = heartbeat_key("deploy", str(deployment.id))
        if not redis_client.exists(key):
            deployment.error = True
            deployment.error_detail = "Reaped: deploy timed out with no progress"
            if (
                deployment.deploy_metadata
                and "installation_id" in deployment.deploy_metadata
                and "statuses_url" in deployment.deploy_metadata
            ):
                access_token = github_app.fetch_installation_access_token(
                    deployment.deploy_metadata["installation_id"]
                )
                post_deployment_status_update(
                    access_token,
                    deployment.deploy_metadata["statuses_url"],
                    "failure",
                    "Deploy timed out.",
                )

    db.session.commit()


@shared_task()
def reap_pods():
    if not current_app.config["KUBERNETES_ENABLED"]:
        return
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
