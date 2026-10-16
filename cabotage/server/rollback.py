"""Roll back to an earlier deployment by redeploying its release.

The ``release-N`` image is already built and bakes in envconsul configs that
point at versioned Consul/Vault keys, so a rollback restores the target
release's code and configuration values without running any build. Ingresses
are snapshotted per release too, so routing reverts unless the rollback keeps
the live routing. Processes missing from the target stop, and the deploy prunes
their scale unless the rollback parks it for a later deploy to restore.
"""

import dataclasses
from collections.abc import Mapping
from typing import TypedDict, cast

from flask import current_app
from requests.exceptions import HTTPError, RequestException

from cabotage._types import assume_not_none
from cabotage.server.models.projects import (
    DEFAULT_POD_CLASS,
    ApplicationEnvironment,
    Deployment,
    Release,
)


@dataclasses.dataclass(frozen=True)
class ProcessScale:
    name: str
    count: int
    pod_class: str


@dataclasses.dataclass
class RollbackPlan:
    target: Deployment
    target_release: Release | None
    current: Deployment | None
    current_release: Release | None
    blockers: list[str] = dataclasses.field(default_factory=list)
    config_added: list[str] = dataclasses.field(default_factory=list)
    config_removed: list[str] = dataclasses.field(default_factory=list)
    config_changed: list[str] = dataclasses.field(default_factory=list)
    release_commands: list[str] = dataclasses.field(default_factory=list)
    hosts_removed: list[str] = dataclasses.field(default_factory=list)
    hosts_added: list[str] = dataclasses.field(default_factory=list)
    ingresses_changed: list[str] = dataclasses.field(default_factory=list)
    live_ingress_release_id: str | None = None
    processes_stopped: list[ProcessScale] = dataclasses.field(default_factory=list)
    processes_started: list[ProcessScale] = dataclasses.field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return not self.blockers

    @property
    def routing_changes(self) -> bool:
        return bool(self.hosts_removed or self.hosts_added or self.ingresses_changed)

    def deploy_metadata(
        self, username: str, *, keep_routing: bool = False, keep_scale: bool = False
    ) -> dict[str, str | int | bool | None]:
        target_release = assume_not_none(
            self.target_release, because="only allowed plans are deployed"
        )
        metadata: dict[str, str | int | bool | None] = {
            "trigger": "rollback",
            "triggered_by": username,
            "rollback_from_version": (
                self.current_release.version if self.current_release else None
            ),
            "rollback_to_version": target_release.version,
        }
        if keep_routing and self.routing_changes and self.live_ingress_release_id:
            metadata["ingresses_from_release_id"] = self.live_ingress_release_id
        if keep_scale and self.processes_stopped:
            metadata["keep_scale"] = True
        return metadata


def rollback_summary(deploy_metadata: Mapping[str, object] | None) -> str | None:
    """One-line description of a rollback deployment, or None for other triggers."""
    meta = deploy_metadata or {}
    if meta.get("trigger") != "rollback":
        return None
    from_version = meta.get("rollback_from_version")
    source = f"v{from_version} → " if from_version is not None else ""
    return (
        f"Rollback {source}v{meta.get('rollback_to_version')} "
        f"by {meta.get('triggered_by', '?')}"
    )


def _release_tag_exists(release: Release) -> bool:
    """False only when the registry says ``release-N`` is gone."""
    from cabotage.celery.tasks.build import BuildkitEnv

    try:
        _ = BuildkitEnv(release.repository_name).verify_registry_tag(
            release.repository_name, f"release-{release.version}"
        )
    except RequestException as exc:
        if (
            isinstance(exc, HTTPError)
            and exc.response is not None
            and exc.response.status_code == 404
        ):
            return False
        current_app.logger.warning("Registry check failed for rollback: %s", exc)
    return True


def environment_is_active(app_env: ApplicationEnvironment) -> bool:
    """False once the app environment or its environment is soft-deleted."""
    return app_env.deleted_at is None and app_env.environment.deleted_at is None


type IngressSnapshots = dict[str, dict[str, object]]


class _HostSnapshot(TypedDict):
    hostname: str


class ParkedScale(TypedDict):
    count: int
    pod_class: str


def _release_ingresses(
    release: Release, app_env: ApplicationEnvironment
) -> IngressSnapshots:
    """Ingress snapshots a deploy of ``release`` applies.

    Releases cut before ingresses were snapshotted deploy the live ingresses.
    """
    snapshots: IngressSnapshots = release.ingresses or {
        ing.name: ing.asdict for ing in app_env.ingresses
    }
    return snapshots


def _ingress_source(deployment: Deployment) -> Release | None:
    """The release whose ingresses ``deployment`` applied."""
    source_id = (deployment.deploy_metadata or {}).get("ingresses_from_release_id")
    if source_id:
        source: Release | None = Release.query.filter_by(id=source_id).first()
        if source is not None:
            return source
    release: Release | None = deployment.release_object
    return release


def deployed_ingresses(deployment: Deployment) -> IngressSnapshots:
    """Ingress snapshots ``deployment`` applies, honoring kept routing."""
    source = _ingress_source(deployment)
    if source is None:
        return {}
    return _release_ingresses(source, deployment.application_environment)


def _routed_hosts(ingresses: IngressSnapshots) -> set[str]:
    hosts: set[str] = set()
    for ingress in ingresses.values():
        if not ingress.get("enabled", True):
            continue
        snapshots = cast("list[_HostSnapshot]", ingress.get("hosts") or [])
        hosts.update(host["hostname"] for host in snapshots)
        tailscale_hostname = ingress.get("tailscale_hostname")
        if isinstance(tailscale_hostname, str) and tailscale_hostname:
            hosts.add(tailscale_hostname)
    return hosts


def _without_id(ingress: Mapping[str, object]) -> dict[str, object]:
    """Ingress settings minus its row id, as release ingress diffs compare."""
    return {key: value for key, value in ingress.items() if key != "id"}


def parked_scale(
    app_env: ApplicationEnvironment, *, before: Deployment | None = None
) -> dict[str, ParkedScale]:
    """Scale that kept-scale rollbacks parked, newest first.

    Only the run of rollbacks since the last other completed deployment
    counts: a normal deploy that also lacks a process means it was dropped.
    """
    query = app_env.deployments.filter_by(complete=True).order_by(
        Deployment.created.desc()
    )
    if before is not None:
        query = query.filter(Deployment.id != before.id)
    parked: dict[str, ParkedScale] = {}
    for deployment in query:
        meta: dict[str, object] = deployment.deploy_metadata or {}
        if meta.get("trigger") != "rollback":
            break
        scales = cast("dict[str, ParkedScale]", meta.get("parked_scale") or {})
        for name, scale in scales.items():
            if name not in parked:
                parked[name] = scale
    return parked


def _process_names(release: Release) -> set[str]:
    names: set[str] = {*release.processes, *release.job_processes}
    return names


def restore_parked_scale(deployment: Deployment) -> list[str]:
    """Give processes returning in ``deployment`` the scale a rollback parked.

    Only fills processes with no scale set; returns the names restored.
    """
    app_env = deployment.application_environment
    release_object: Release | None = deployment.release_object
    release = assume_not_none(release_object, because="deploys need a release")
    counts: dict[str, int] = dict(app_env.process_counts or {})
    pod_classes: dict[str, str] = dict(app_env.process_pod_classes or {})
    restored: list[str] = []
    parked = parked_scale(app_env, before=deployment)
    for name in sorted(_process_names(release)):
        scale = parked.get(name)
        if scale is None or name in counts:
            continue
        counts[name] = scale["count"]
        if name not in pod_classes:
            pod_classes[name] = scale["pod_class"]
        restored.append(name)
    if restored:
        app_env.process_counts = counts
        app_env.process_pod_classes = pod_classes
    return restored


def prune_stale_scale(deployment: Deployment, active: set[str]) -> list[str]:
    """Drop scale for processes ``deployment`` doesn't run; park it if kept.

    Returns the pruned process names.
    """
    app_env = deployment.application_environment
    counts: dict[str, int] = dict(app_env.process_counts or {})
    pod_classes: dict[str, str] = dict(app_env.process_pod_classes or {})
    stale = sorted((counts.keys() | pod_classes.keys()) - active)
    if not stale:
        return []
    meta: dict[str, object] = deployment.deploy_metadata or {}
    if meta.get("keep_scale"):
        parked: dict[str, ParkedScale] = {
            name: {
                "count": counts.get(name, 0),
                "pod_class": pod_classes.get(name, DEFAULT_POD_CLASS),
            }
            for name in stale
        }
        deployment.deploy_metadata = {**meta, "parked_scale": parked}
    app_env.process_counts = {k: v for k, v in counts.items() if k in active}
    app_env.process_pod_classes = {k: v for k, v in pod_classes.items() if k in active}
    return stale


def _live_scale(app_env: ApplicationEnvironment, name: str) -> ProcessScale:
    counts: dict[str, int] = app_env.process_counts or {}
    pod_classes: dict[str, str] = app_env.process_pod_classes or {}
    return ProcessScale(
        name, counts.get(name, 0), pod_classes.get(name, DEFAULT_POD_CLASS)
    )


def plan_rollback(target: Deployment) -> RollbackPlan:
    current: Deployment | None = (
        target.application_environment.latest_deployment_completed
    )
    target_release: Release | None = target.release_object
    current_release: Release | None = current.release_object if current else None
    plan = RollbackPlan(target, target_release, current, current_release)

    if not target.complete:
        plan.blockers.append("Only successful deployments can be rolled back to.")
    if target_release is None:
        plan.blockers.append("The release for this deployment no longer exists.")
        return plan
    if current_release is not None and current_release.id == target_release.id:
        plan.blockers.append(f"Release v{target_release.version} is already live.")
    if target_release.deposed:
        plan.blockers.append(
            f"Release v{target_release.version} uses an image or configuration "
            "that no longer exists."
        )
    elif current_app.config["KUBERNETES_ENABLED"] and not _release_tag_exists(
        target_release
    ):
        plan.blockers.append(
            f"Image release-{target_release.version} has been pruned from the registry."
        )

    target_config: dict[str, object] = target_release.configuration or {}
    current_config: dict[str, object] = (
        current_release.configuration or {} if current_release else {}
    )
    plan.config_added = sorted(target_config.keys() - current_config.keys())
    plan.config_removed = sorted(current_config.keys() - target_config.keys())
    plan.config_changed = sorted(
        name
        for name in target_config.keys() & current_config.keys()
        if target_config[name] != current_config[name]
    )
    plan.release_commands = sorted(
        [*target_release.release_commands, *target_release.postdeploy_commands]
    )

    app_env = target.application_environment
    target_ingresses = _release_ingresses(target_release, app_env)
    live_ingresses = deployed_ingresses(current) if current else {}
    live_source = _ingress_source(current) if current else None
    plan.live_ingress_release_id = str(live_source.id) if live_source else None
    target_hosts = _routed_hosts(target_ingresses)
    live_hosts = _routed_hosts(live_ingresses)
    plan.hosts_removed = sorted(live_hosts - target_hosts)
    plan.hosts_added = sorted(target_hosts - live_hosts)
    plan.ingresses_changed = sorted(
        name
        for name in target_ingresses.keys() | live_ingresses.keys()
        if _without_id(target_ingresses.get(name) or {})
        != _without_id(live_ingresses.get(name) or {})
    )

    target_processes = _process_names(target_release)
    live_processes = _process_names(current_release) if current_release else set()
    plan.processes_stopped = [
        _live_scale(app_env, name) for name in sorted(live_processes - target_processes)
    ]
    parked = parked_scale(app_env)
    plan.processes_started = [
        ProcessScale(name, parked[name]["count"], parked[name]["pod_class"])
        if name in parked and name not in (app_env.process_counts or {})
        else _live_scale(app_env, name)
        for name in sorted(target_processes - live_processes)
    ]
    return plan
