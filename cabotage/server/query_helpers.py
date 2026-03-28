"""Shared query helpers for batch-loading status sets and related objects.

These helpers extract duplicated N+1-avoidance patterns from view functions
into reusable functions.
"""

import uuid as _uuid

from sqlalchemy import and_, case, func, or_

from cabotage.server import db
from cabotage.server.models.projects import (
    ApplicationEnvironment,
    Deployment,
    Image,
    Release,
)


def compute_app_status_sets(app_ids):
    """Batch-compute deployed/errored/building status for a list of application IDs.

    Queries via the default ApplicationEnvironment (k8s_identifier IS NULL).

    Returns dict with keys: deployed_app_ids, errored_app_ids, building_app_ids
    """
    deployed_app_ids = set()
    errored_app_ids = set()
    building_app_ids = set()

    if not app_ids:
        return {
            "deployed_app_ids": deployed_app_ids,
            "errored_app_ids": errored_app_ids,
            "building_app_ids": building_app_ids,
        }

    # Apps with any running or completed deployment
    deployed_app_ids = {
        row[0]
        for row in db.session.query(Deployment.application_id)
        .join(ApplicationEnvironment)
        .filter(
            Deployment.application_id.in_(app_ids),
            ApplicationEnvironment.deleted_at.is_(None),
            ApplicationEnvironment.k8s_identifier.is_(None),
            or_(
                Deployment.complete == True,  # noqa: E712
                and_(
                    Deployment.complete == False,  # noqa: E712
                    Deployment.error == False,  # noqa: E712
                ),
            ),
        )
        .distinct()
    }

    # Apps where latest error image version > latest built image version
    error_sub = (
        db.session.query(
            Image.application_id,
            func.max(Image.version).label("v"),
        )
        .join(ApplicationEnvironment)
        .filter(
            Image.application_id.in_(app_ids),
            ApplicationEnvironment.deleted_at.is_(None),
            ApplicationEnvironment.k8s_identifier.is_(None),
            Image.error == True,  # noqa: E712
        )
        .group_by(Image.application_id)
        .subquery()
    )
    built_sub = (
        db.session.query(
            Image.application_id,
            func.max(Image.version).label("v"),
        )
        .join(ApplicationEnvironment)
        .filter(
            Image.application_id.in_(app_ids),
            ApplicationEnvironment.deleted_at.is_(None),
            ApplicationEnvironment.k8s_identifier.is_(None),
            Image.built == True,  # noqa: E712
        )
        .group_by(Image.application_id)
        .subquery()
    )
    errored_app_ids = {
        row[0]
        for row in db.session.query(error_sub.c.application_id)
        .outerjoin(
            built_sub,
            error_sub.c.application_id == built_sub.c.application_id,
        )
        .filter(or_(built_sub.c.v.is_(None), error_sub.c.v > built_sub.c.v))
    }

    # Apps with any in-progress image build
    building_app_ids = {
        row[0]
        for row in db.session.query(Image.application_id)
        .join(ApplicationEnvironment)
        .filter(
            Image.application_id.in_(app_ids),
            ApplicationEnvironment.deleted_at.is_(None),
            ApplicationEnvironment.k8s_identifier.is_(None),
            Image.built == False,  # noqa: E712
            Image.error == False,  # noqa: E712
        )
        .distinct()
    }

    return {
        "deployed_app_ids": deployed_app_ids,
        "errored_app_ids": errored_app_ids,
        "building_app_ids": building_app_ids,
    }


def compute_ae_status_sets(ae_ids):
    """Batch-compute deployment/image status for a list of ApplicationEnvironment IDs.

    Returns dict with keys: deploying_ae_ids, completed_ae_ids, running_ae_ids,
    building_ae_ids, errored_ae_ids, last_deploy_by_ae, deploy_count
    """
    deploying_ae_ids = set()
    completed_ae_ids = set()
    running_ae_ids = set()
    building_ae_ids = set()
    errored_ae_ids = set()
    last_deploy_by_ae = {}
    deploy_count = 0

    if not ae_ids:
        return {
            "deploying_ae_ids": deploying_ae_ids,
            "completed_ae_ids": completed_ae_ids,
            "running_ae_ids": running_ae_ids,
            "building_ae_ids": building_ae_ids,
            "errored_ae_ids": errored_ae_ids,
            "last_deploy_by_ae": last_deploy_by_ae,
            "deploy_count": deploy_count,
        }

    # Latest deployment status per ae (for deploying/running)
    latest_deploy_created_sub = (
        db.session.query(
            Deployment.application_environment_id,
            func.max(Deployment.created).label("max_created"),
        )
        .filter(Deployment.application_environment_id.in_(ae_ids))
        .group_by(Deployment.application_environment_id)
        .subquery()
    )
    latest_deploys = (
        db.session.query(
            Deployment.application_environment_id,
            Deployment.complete,
            Deployment.error,
        )
        .join(
            latest_deploy_created_sub,
            and_(
                Deployment.application_environment_id
                == latest_deploy_created_sub.c.application_environment_id,
                Deployment.created == latest_deploy_created_sub.c.max_created,
            ),
        )
        .all()
    )
    deploying_ae_ids = {r[0] for r in latest_deploys if not r[1] and not r[2]}
    running_ae_ids = {r[0] for r in latest_deploys if r[1] or not r[2]}

    # Completed deploy stats: count + last deploy per ae
    deploy_stats = (
        db.session.query(
            Deployment.application_environment_id,
            func.count(Deployment.id),
            func.max(Deployment.created),
        )
        .filter(
            Deployment.application_environment_id.in_(ae_ids),
            Deployment.complete == True,  # noqa: E712
        )
        .group_by(Deployment.application_environment_id)
        .all()
    )
    completed_ae_ids = {r[0] for r in deploy_stats}
    last_deploy_by_ae = {r[0]: r[2] for r in deploy_stats}
    deploy_count = sum(r[1] for r in deploy_stats)

    # Image stats: one query for error, built, and building checks
    image_stats = (
        db.session.query(
            Image.application_environment_id,
            func.max(
                case((Image.error == True, Image.version), else_=None)  # noqa: E712
            ).label("max_error_v"),
            func.max(
                case((Image.built == True, Image.version), else_=None)  # noqa: E712
            ).label("max_built_v"),
            func.count(
                case(
                    (
                        and_(
                            Image.built == False,  # noqa: E712
                            Image.error == False,  # noqa: E712
                        ),
                        1,
                    )
                )
            ).label("building_count"),
        )
        .filter(Image.application_environment_id.in_(ae_ids))
        .group_by(Image.application_environment_id)
        .all()
    )
    errored_ae_ids = {
        r[0] for r in image_stats if r[1] is not None and (r[2] is None or r[1] > r[2])
    }
    building_ae_ids = {r[0] for r in image_stats if r[3] > 0}

    return {
        "deploying_ae_ids": deploying_ae_ids,
        "completed_ae_ids": completed_ae_ids,
        "running_ae_ids": running_ae_ids,
        "building_ae_ids": building_ae_ids,
        "errored_ae_ids": errored_ae_ids,
        "last_deploy_by_ae": last_deploy_by_ae,
        "deploy_count": deploy_count,
    }


class RelatedObjectResolver:
    """Caches Release/Image lookups from JSONB foreign keys.

    Avoids cascading queries like deployment.release_object → release.image_object.
    """

    def __init__(self, images=None, releases=None):
        self._release_cache = {}
        self._image_cache = {i.id: i for i in (images or [])}
        self._all_releases = releases or []

    def warm_caches(self, deployments, releases):
        """Pre-resolve all Release/Image references from deployments and releases."""
        for d in deployments:
            self._get_release(d)
        for r in releases:
            self._get_image_for_release(r)

    def build_lookup_dicts(self):
        """Return (release_by_id, image_by_id) dicts keyed by string UUID."""
        release_by_id = {str(k): v for k, v in self._release_cache.items() if v}
        image_by_id = {str(k): v for k, v in self._image_cache.items() if v}
        return release_by_id, image_by_id

    def get_release(self, deploy):
        """Get the Release object referenced by a Deployment's JSONB field."""
        return self._get_release(deploy)

    def get_image_for_release(self, rel):
        """Get the Image object referenced by a Release's JSONB field."""
        return self._get_image_for_release(rel)

    def _get_release(self, deploy):
        if not deploy or not deploy.release:
            return None
        rid = deploy.release.get("id")
        if not rid:
            return None
        rid = _uuid.UUID(rid) if isinstance(rid, str) else rid
        if rid not in self._release_cache:
            found = next((r for r in self._all_releases if r.id == rid), None)
            if found is None:
                found = Release.query.get(rid)
            self._release_cache[rid] = found
        return self._release_cache[rid]

    def _get_image_for_release(self, rel):
        if not rel or not rel.image:
            return None
        iid = rel.image.get("id")
        if not iid:
            return None
        iid = _uuid.UUID(iid) if isinstance(iid, str) else iid
        if iid not in self._image_cache:
            self._image_cache[iid] = Image.query.get(iid)
        return self._image_cache[iid]


def extract_latest_variants(images, releases, deployments):
    """Extract latest_* variants from pre-fetched lists.

    Returns dict with keys: latest_image, latest_image_built, latest_image_error,
    latest_image_building, latest_release, latest_release_built, latest_release_building,
    latest_deployment, latest_deployment_completed, has_releases
    """
    return {
        "latest_image": images[0] if images else None,
        "latest_image_built": next((i for i in images if i.built), None),
        "latest_image_error": next((i for i in images if i.error), None),
        "latest_image_building": next(
            (i for i in images if not i.built and not i.error), None
        ),
        "latest_release": releases[0] if releases else None,
        "latest_release_built": next((r for r in releases if r.built), None),
        "latest_release_building": next(
            (r for r in releases if not r.built and not r.error), None
        ),
        "latest_deployment": deployments[0] if deployments else None,
        "latest_deployment_completed": next(
            (d for d in deployments if d.complete), None
        ),
        "has_releases": len(releases) > 0,
    }


def compute_process_counts(releases, resolver):
    """Compute service process count per release (excludes release/postdeploy commands).

    Returns {str(release_id): int}.
    """
    release_proc_counts = {}
    for r in releases:
        img = resolver.get_image_for_release(r)
        if r.built and img and img.processes:
            release_proc_counts[str(r.id)] = sum(
                1
                for k in img.processes
                if not k.startswith("release") and not k.startswith("postdeploy")
            )
    return release_proc_counts


_INGRESS_SETTING_KEYS = {
    "enabled",
    "ingress_class_name",
    "backend_protocol",
    "proxy_connect_timeout",
    "proxy_read_timeout",
    "proxy_send_timeout",
    "proxy_body_size",
    "client_body_buffer_size",
    "proxy_request_buffering",
    "session_affinity",
    "use_regex",
    "allow_annotations",
    "extra_annotations",
    "cluster_issuer",
    "force_ssl_redirect",
    "service_upstream",
    "tailscale_hostname",
    "tailscale_funnel",
    "tailscale_tags",
}


def _diff_config_item(old, new):
    """Return list of human-readable descriptions for a changed config entry."""
    changes = []
    if old.get("version_id") != new.get("version_id"):
        changes.append("value changed")
    if old.get("secret") != new.get("secret"):
        changes.append("marked secret" if new.get("secret") else "unmarked secret")
    if old.get("buildtime") != new.get("buildtime"):
        changes.append(
            "marked buildtime" if new.get("buildtime") else "unmarked buildtime"
        )
    return changes


def _strip_id(d):
    """Return a dict copy without the 'id' key (for comparing snapshots)."""
    return {k: v for k, v in d.items() if k != "id"}


def _diff_ingress_item(old, new):
    """Return list of human-readable descriptions for a changed ingress entry."""
    parts = []

    # --- Hosts: added, removed, and property changes on existing hosts ---
    old_hosts_by_name = {h["hostname"]: h for h in old.get("hosts", [])}
    new_hosts_by_name = {h["hostname"]: h for h in new.get("hosts", [])}
    h_added = sorted(set(new_hosts_by_name) - set(old_hosts_by_name))
    h_removed = sorted(set(old_hosts_by_name) - set(new_hosts_by_name))
    if h_added:
        parts.append("hosts added: " + ", ".join(h_added))
    if h_removed:
        parts.append("hosts removed: " + ", ".join(h_removed))
    # Check property changes on hosts that exist in both
    h_changed = []
    for hostname in sorted(set(old_hosts_by_name) & set(new_hosts_by_name)):
        oh = _strip_id(old_hosts_by_name[hostname])
        nh = _strip_id(new_hosts_by_name[hostname])
        if oh != nh:
            diffs = [
                f"{k}: {oh.get(k)} → {nh.get(k)}" for k in nh if oh.get(k) != nh.get(k)
            ]
            h_changed.append(f"{hostname} ({', '.join(diffs)})")
    if h_changed:
        parts.append("hosts changed: " + ", ".join(h_changed))

    # --- Paths: added, removed, and property changes on existing paths ---
    old_paths_by_path = {p["path"]: p for p in old.get("paths", [])}
    new_paths_by_path = {p["path"]: p for p in new.get("paths", [])}
    p_added = sorted(set(new_paths_by_path) - set(old_paths_by_path))
    p_removed = sorted(set(old_paths_by_path) - set(new_paths_by_path))
    if p_added:
        parts.append(
            "paths added: "
            + ", ".join(
                f"{p} → {new_paths_by_path[p].get('target_process_name', '')}"
                for p in p_added
            )
        )
    if p_removed:
        parts.append(
            "paths removed: "
            + ", ".join(
                f"{p} → {old_paths_by_path[p].get('target_process_name', '')}"
                for p in p_removed
            )
        )
    p_changed = []
    for path in sorted(set(old_paths_by_path) & set(new_paths_by_path)):
        op = _strip_id(old_paths_by_path[path])
        np = _strip_id(new_paths_by_path[path])
        if op != np:
            diffs = [
                f"{k}: {op.get(k)} → {np.get(k)}" for k in np if op.get(k) != np.get(k)
            ]
            p_changed.append(f"{path} ({', '.join(diffs)})")
    if p_changed:
        parts.append("paths changed: " + ", ".join(p_changed))

    # --- Top-level settings ---
    changed_settings = sorted(
        k for k in _INGRESS_SETTING_KEYS if old.get(k) != new.get(k)
    )
    if changed_settings:
        parts.append("settings: " + ", ".join(changed_settings))
    return parts


def compute_release_change_details(releases, deployments=None):
    """Compute granular change descriptions for a list of releases.

    For each release that has config or ingress changes, diffs the release's
    snapshot against the last successfully deployed release to describe what
    specifically changed.  This mirrors what ``create_release`` compared
    against (the ``latest_deployment_completed`` at creation time).

    Falls back to the previous built release if deployment data isn't
    available.

    Takes releases sorted newest-first and an optional list of deployments
    (sorted newest-first) for finding the deployed baseline.

    Returns:
        {str(release_id): {"config": {name: [descriptions]},
                           "ingress": {name: [descriptions]}}}
    """
    # Build a map of release_id → Release for deployed releases
    deployed_release_by_id = {}
    if deployments:
        for d in deployments:
            if d.complete and not d.error and d.release:
                rid = d.release.get("id")
                if rid:
                    deployed_release_by_id[str(rid)] = d

    result = {}
    for i, rel in enumerate(releases):
        cfg_ch = rel.configuration_changes or {}
        ing_ch = rel.ingress_changes or {}
        cfg_changed = cfg_ch.get("changed", [])
        ing_changed = ing_ch.get("changed", [])
        if not cfg_changed and not ing_changed:
            continue

        # Find the baseline: walk backwards through releases to find
        # the most recent one that was successfully deployed.  This
        # matches what create_release compared against (the
        # latest_deployment_completed at creation time).
        prev_rel = None
        for j in range(i + 1, len(releases)):
            candidate = releases[j]
            if str(candidate.id) in deployed_release_by_id:
                prev_rel = candidate
                break
        # Fall back to immediate predecessor if no deployed release found
        if prev_rel is None and i + 1 < len(releases):
            prev_rel = releases[i + 1]

        prev_cfg = (prev_rel.configuration or {}) if prev_rel else {}
        prev_ing = (prev_rel.ingresses or {}) if prev_rel else {}
        cur_cfg = rel.configuration or {}
        cur_ing = rel.ingresses or {}
        details = {"config": {}, "ingress": {}}

        for name in cfg_changed:
            field_changes = _diff_config_item(
                prev_cfg.get(name, {}), cur_cfg.get(name, {})
            )
            if field_changes:
                details["config"][name] = field_changes

        for name in ing_changed:
            parts = _diff_ingress_item(prev_ing.get(name, {}), cur_ing.get(name, {}))
            if parts:
                details["ingress"][name] = parts

        result[str(rel.id)] = details
    return result


def split_image_processes(image):
    """Split image.processes into (service_procs, release_cmds, postdeploy_cmds).

    Mirrors Release.processes / release_commands / postdeploy_commands without
    triggering an image_object query.
    """
    if not image or not image.processes:
        return {}, {}, {}
    all_procs = image.processes
    service_procs = {
        k: v
        for k, v in all_procs.items()
        if not (k.startswith("release") or k.startswith("postdeploy"))
    }
    release_cmds = {k: v for k, v in all_procs.items() if k.startswith("release")}
    postdeploy_cmds = {k: v for k, v in all_procs.items() if k.startswith("postdeploy")}
    return service_procs, release_cmds, postdeploy_cmds
