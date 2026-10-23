"""Prometheus metrics for pipeline stages.

Emits histograms and counters via Pushgateway when configured.
Graceful no-op if PUSHGATEWAY_URL is not set — all public functions
return immediately.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Counter, Histogram, push_to_gateway

if TYPE_CHECKING:
    from cabotage.server.models.projects import Deployment, Image, Release

logger = logging.getLogger(__name__)

_REGISTRY = CollectorRegistry()
_LABELS = ["org", "project", "app", "env", "trigger", "status"]

_STAGE_HISTOGRAMS: dict[str, Histogram] = {
    "image": Histogram(
        "cabotage_image_build_duration_seconds",
        "Duration of image builds",
        _LABELS,
        registry=_REGISTRY,
    ),
    "release": Histogram(
        "cabotage_release_build_duration_seconds",
        "Duration of release builds",
        _LABELS,
        registry=_REGISTRY,
    ),
    "deploy": Histogram(
        "cabotage_deploy_duration_seconds",
        "Duration of deployments",
        _LABELS,
        registry=_REGISTRY,
    ),
}

_STAGE_TOTAL = Counter(
    "cabotage_pipeline_stage_total",
    "Count of pipeline stage completions",
    [*_LABELS, "stage"],
    registry=_REGISTRY,
)


def _resolve_labels(model: Image | Release | Deployment) -> dict[str, str]:
    """Extract label values from an Image, Release, or Deployment."""
    app_env = model.application_environment
    application = getattr(model, "application", None) or app_env.application
    project = application.project
    org = project.organization

    trigger = getattr(model, "trigger_type", "manual")
    status = "error" if model.error else "success"

    return {
        "org": org.slug,
        "project": project.slug,
        "app": application.slug,
        "env": app_env.environment.slug if app_env.environment else "default",
        "trigger": trigger,
        "status": status,
    }


def _push_metrics(
    stage: str, duration_seconds: float | None, labels: dict[str, str]
) -> None:
    """Record and optionally push metrics."""
    from flask import current_app

    pushgateway_url = current_app.config.get("PUSHGATEWAY_URL")
    if not pushgateway_url:
        # No Pushgateway configured — skip recording entirely to avoid
        # accumulating unbounded label state in worker memory.
        return

    histogram = _STAGE_HISTOGRAMS.get(stage)
    if histogram and duration_seconds is not None:
        histogram.labels(**labels).observe(duration_seconds)

    _STAGE_TOTAL.labels(**labels, stage=stage).inc()

    try:
        push_to_gateway(
            pushgateway_url,
            job="cabotage_pipeline",
            registry=_REGISTRY,
        )
    except Exception:
        logger.warning("Failed to push metrics to Pushgateway", exc_info=True)


def _should_push() -> bool:
    """Check if metrics should be pushed (Pushgateway configured)."""
    from flask import current_app

    return bool(current_app.config.get("PUSHGATEWAY_URL"))


def record_image_metrics(image: Image) -> None:
    """Record metrics for a completed image build."""
    if not _should_push():
        return
    _push_metrics("image", image.duration_seconds, _resolve_labels(image))


def record_release_metrics(release: Release) -> None:
    """Record metrics for a completed release build."""
    if not _should_push():
        return
    _push_metrics("release", release.duration_seconds, _resolve_labels(release))


def record_deploy_metrics(deployment: Deployment) -> None:
    """Record metrics for a completed deployment."""
    if not _should_push():
        return
    _push_metrics("deploy", deployment.duration_seconds, _resolve_labels(deployment))
