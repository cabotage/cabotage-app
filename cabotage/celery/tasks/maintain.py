import datetime
import logging

import kubernetes

from celery import shared_task
from flask import current_app

from cabotage.server import db, github_app, kubernetes as kubernetes_ext
from cabotage.server.models.projects import Deployment, Image, Release
from cabotage.celery.tasks.notify import (
    dispatch_autodeploy_notification,
    dispatch_pipeline_notification,
)
from cabotage.utils.build_log_stream import (
    get_redis_client,
    heartbeat_key,
    publish_end,
    stream_key,
)
from cabotage.utils.github import cabotage_url, post_deployment_status_update

log = logging.getLogger(__name__)


def _dispatch_reap_failure(obj, obj_type, notification_type):
    """Send a failure notification for a reaped build/deploy."""
    try:
        app = obj.application
        if not app:
            return

        metadata = (
            getattr(obj, "image_metadata", None)
            or getattr(obj, "release_metadata", None)
            or getattr(obj, "deploy_metadata", None)
            or {}
        )

        if metadata.get("auto_deploy"):
            image_id = metadata.get("source_image_id", str(obj.id))
            stage_map = {
                "Image": "image_failed",
                "Release": "release_failed",
                "Deployment": "deploy_failed",
            }
            stage = stage_map.get(obj_type, "deploy_failed")
            dispatch_autodeploy_notification(
                stage,
                image_id,
                app,
                obj.application_environment,
                error=obj.error_detail,
                image_url=cabotage_url(app, f"images/{image_id}"),
                image_metadata=metadata,
            )
        else:
            dispatch_pipeline_notification.delay(
                notification_type,
                obj_type,
                str(obj.id),
                str(app.project.organization_id),
                str(app.id),
                str(obj.application_environment_id)
                if obj.application_environment_id
                else None,
                error=obj.error_detail,
            )
    except Exception:
        log.warning(
            "Failed to dispatch reap notification for %s %s",
            obj_type,
            obj.id,
            exc_info=True,
        )


@shared_task()
def reap_stale_builds():
    """Find stuck image builds, release builds, and deploys with no heartbeat."""
    redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=90
    )

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
            if image.build_job_id:
                try:
                    log_key = stream_key("image", image.build_job_id)
                    publish_end(redis_client, log_key, error=True)
                except Exception:
                    log.warning(
                        "Failed to publish log stream end for reaped image %s",
                        image.id,
                        exc_info=True,
                    )
            if (
                image.image_metadata
                and "installation_id" in image.image_metadata
                and "statuses_url" in image.image_metadata
                and not image.image_metadata.get("branch_deploy")
            ):
                try:
                    access_token = github_app.fetch_installation_access_token(
                        image.image_metadata["installation_id"]
                    )
                    post_deployment_status_update(
                        access_token,
                        image.image_metadata["statuses_url"],
                        "failure",
                        "Image build timed out.",
                    )
                except Exception:
                    log.warning(
                        "Failed to post GitHub status for reaped image %s",
                        image.id,
                        exc_info=True,
                    )
            _dispatch_reap_failure(image, "Image", "pipeline.image_build")

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
            if release.build_job_id:
                try:
                    log_key = stream_key("release", release.build_job_id)
                    publish_end(redis_client, log_key, error=True)
                except Exception:
                    log.warning(
                        "Failed to publish log stream end for reaped release %s",
                        release.id,
                        exc_info=True,
                    )
            if (
                release.release_metadata
                and "installation_id" in release.release_metadata
                and "statuses_url" in release.release_metadata
                and not release.release_metadata.get("branch_deploy")
            ):
                try:
                    access_token = github_app.fetch_installation_access_token(
                        release.release_metadata["installation_id"]
                    )
                    post_deployment_status_update(
                        access_token,
                        release.release_metadata["statuses_url"],
                        "failure",
                        "Release build timed out.",
                    )
                except Exception:
                    log.warning(
                        "Failed to post GitHub status for reaped release %s",
                        release.id,
                        exc_info=True,
                    )
            _dispatch_reap_failure(release, "Release", "pipeline.release")

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
            if deployment.job_id:
                try:
                    log_key = stream_key("deploy", deployment.job_id)
                    publish_end(redis_client, log_key, error=True)
                except Exception:
                    log.warning(
                        "Failed to publish log stream end for reaped deployment %s",
                        deployment.id,
                        exc_info=True,
                    )
            if (
                deployment.deploy_metadata
                and "installation_id" in deployment.deploy_metadata
                and "statuses_url" in deployment.deploy_metadata
                and not deployment.deploy_metadata.get("branch_deploy")
            ):
                try:
                    access_token = github_app.fetch_installation_access_token(
                        deployment.deploy_metadata["installation_id"]
                    )
                    post_deployment_status_update(
                        access_token,
                        deployment.deploy_metadata["statuses_url"],
                        "failure",
                        "Deploy timed out.",
                    )
                except Exception:
                    log.warning(
                        "Failed to post GitHub status for reaped deployment %s",
                        deployment.id,
                        exc_info=True,
                    )
            _dispatch_reap_failure(deployment, "Deployment", "pipeline.deploy")

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
