"""Celery task to reap completed/failed CronJob-spawned Jobs.

Finds K8s Jobs labelled resident-job.cabotage.io=true that are no longer
Pending/Running, records metadata into the job_logs table, then deletes
them from the cluster.
"""

import datetime
import os

import kubernetes
from kubernetes.client.rest import ApiException
from sqlalchemy.exc import IntegrityError

from celery import shared_task
from flask import current_app

from cabotage.server import db, kubernetes as kubernetes_ext
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    JobLog,
    Project,
)

DEFAULT_REAP_LIMIT = 10


def _is_finished(job):
    """Return True if the Job has a Complete or Failed condition."""
    if not job.status.conditions:
        return False
    for cond in job.status.conditions:
        if cond.type in ("Complete", "Failed") and cond.status == "True":
            return True
    return False


def _is_succeeded(job):
    """Return True if the Job completed successfully."""
    if not job.status.conditions:
        return False
    for cond in job.status.conditions:
        if cond.type == "Complete" and cond.status == "True":
            return True
    return False


def _parse_datetime(value):
    """Parse a K8s datetime value (may be a datetime or string)."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _extract_resources(job):
    """Extract CPU/memory requests and limits from the process container."""
    process_name = (job.metadata.labels or {}).get("process")
    containers = (
        job.spec.template.spec.containers
        if job.spec.template and job.spec.template.spec
        else []
    )
    for container in containers or []:
        if container.name == process_name:
            res = container.resources
            if res is None:
                return None
            result = {}
            if res.requests:
                result["requests"] = {k: str(v) for k, v in res.requests.items()}
            if res.limits:
                result["limits"] = {k: str(v) for k, v in res.limits.items()}
            return result or None
    return None


def _resolve_app_env(labels):
    """Look up Application and ApplicationEnvironment from job labels."""
    org_slug = labels.get("organization")
    project_slug = labels.get("project")
    app_slug = labels.get("application")
    env_slug = labels.get("environment")

    if not all([org_slug, project_slug, app_slug]):
        return None, None

    application = (
        Application.query.join(Project)
        .filter(
            Project.organization.has(slug=org_slug),
            Project.slug == project_slug,
            Application.slug == app_slug,
        )
        .first()
    )
    if application is None:
        return None, None

    if env_slug:
        app_env = (
            ApplicationEnvironment.query.join(Environment)
            .filter(
                ApplicationEnvironment.application_id == application.id,
                Environment.slug == env_slug,
                ApplicationEnvironment.deleted_at.is_(None),
            )
            .first()
        )
    else:
        app_env = ApplicationEnvironment.query.filter(
            ApplicationEnvironment.application_id == application.id,
            ApplicationEnvironment.deleted_at.is_(None),
        ).first()

    return application, app_env


def _reap_limit():
    try:
        return int(os.environ.get("CABOTAGE_JOBS_REAPED_PER_RUN", DEFAULT_REAP_LIMIT))
    except (ValueError, TypeError):
        return DEFAULT_REAP_LIMIT


@shared_task()
def reap_finished_jobs():
    """Find finished CronJob-spawned Jobs, log metadata, and delete them."""
    if not current_app.config.get("KUBERNETES_ENABLED"):
        return

    api_client = kubernetes_ext.kubernetes_client
    batch_api = kubernetes.client.BatchV1Api(api_client)

    label_selector = "resident-job.cabotage.io=true"
    limit = _reap_limit()

    try:
        jobs = batch_api.list_job_for_all_namespaces(
            label_selector=label_selector,
        )
    except ApiException as exc:
        current_app.logger.error(f"Failed to list jobs: {exc}")
        return

    reaped = 0
    for job in jobs.items:
        if reaped >= limit:
            break

        if not _is_finished(job):
            continue

        labels = job.metadata.labels or {}
        annotations = job.metadata.annotations or {}
        namespace = job.metadata.namespace
        job_name = job.metadata.name

        application, app_env = _resolve_app_env(labels)
        if application is None or app_env is None:
            current_app.logger.warning(
                f"Could not resolve app/env for Job/{job_name} "
                f"in {namespace}, deleting without logging"
            )
            _delete_job(batch_api, job_name, namespace)
            reaped += 1
            continue

        start_time = _parse_datetime(job.status.start_time)
        completion_time = _parse_datetime(job.status.completion_time)
        duration = None
        if start_time and completion_time:
            duration = int((completion_time - start_time).total_seconds())

        schedule_ts_raw = annotations.get(
            "batch.kubernetes.io/cronjob-scheduled-timestamp"
        )
        schedule_timestamp = _parse_datetime(schedule_ts_raw)

        release_version = None
        try:
            release_version = int(labels.get("release", ""))
        except (ValueError, TypeError):
            pass

        resources = _extract_resources(job)

        job_log = JobLog(
            application_id=application.id,
            application_environment_id=app_env.id,
            process_name=labels.get("process", "unknown"),
            job_name=job_name,
            namespace=namespace,
            schedule_timestamp=schedule_timestamp,
            start_time=start_time,
            completion_time=completion_time,
            duration_seconds=duration,
            succeeded=_is_succeeded(job),
            pods_active=job.status.active or 0,
            pods_succeeded=job.status.succeeded or 0,
            pods_failed=job.status.failed or 0,
            release_version=release_version,
            deployment_id=labels.get("deployment"),
            labels=labels,
            resources=resources,
        )
        db.session.add(job_log)
        try:
            db.session.commit()
        except IntegrityError:
            # Another reaper already logged this job — that's fine.
            db.session.rollback()

        _delete_job(batch_api, job_name, namespace)
        reaped += 1

    if reaped:
        current_app.logger.info(f"Reaped {reaped} finished job(s)")


def _delete_job(batch_api, name, namespace):
    """Delete a Job and its dependent pods."""
    try:
        batch_api.delete_namespaced_job(
            name,
            namespace,
            propagation_policy="Background",
        )
    except ApiException as exc:
        if exc.status != 404:
            current_app.logger.warning(
                f"Failed to delete Job/{name} in {namespace}: {exc}"
            )
