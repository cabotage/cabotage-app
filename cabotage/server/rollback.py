"""Roll back to an earlier deployment by redeploying its release.

The ``release-N`` image is already built and bakes in envconsul configs that
point at versioned Consul/Vault keys, so a rollback restores the target
release's code and configuration values without running any build.
"""

import dataclasses
from collections.abc import Mapping

from flask import current_app
from requests.exceptions import HTTPError, RequestException

from cabotage._types import assume_not_none
from cabotage.server.models.projects import Deployment, Release


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

    @property
    def allowed(self) -> bool:
        return not self.blockers

    def deploy_metadata(self, username: str) -> dict[str, str | int | None]:
        target_release = assume_not_none(
            self.target_release, because="only allowed plans are deployed"
        )
        return {
            "trigger": "rollback",
            "triggered_by": username,
            "rollback_from_version": (
                self.current_release.version if self.current_release else None
            ),
            "rollback_to_version": target_release.version,
        }


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
    return plan
