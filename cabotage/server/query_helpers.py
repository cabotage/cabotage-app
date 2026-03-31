"""Shared query helpers for batch-loading status sets and related objects.

These helpers extract duplicated N+1-avoidance patterns from view functions
into reusable functions.
"""

import uuid as _uuid

from sqlalchemy import and_, case, func, or_

from cabotage.server import db
from cabotage.server.models.auth import Organization, User
from sqlalchemy_continuum import version_class

from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Configuration,
    Deployment,
    Environment,
    EnvironmentConfiguration,
    Image,
    Ingress,
    IngressHost,
    IngressPath,
    Release,
    activity_plugin,
)

Activity = activity_plugin.activity_cls


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


def query_app_activities(
    application, app_env, verb=None, object_type=None, page=1, per_page=30
):
    """Query Activity records related to an application and its app_env.

    Uses subqueries to find activities for all related objects (configs,
    ingresses, images, releases, deployments) including deleted ones,
    without building a huge IN clause in Python.

    Returns a paginated result.
    """
    filters = [
        Activity.object_type == "Application",
        Activity.object_id == str(application.id),
    ]

    if app_env:
        ae_id = str(app_env.id)
        app_id = str(application.id)

        # Subqueries for related object IDs, scoped to this app_env.
        # For configs, use application_id + UNION with app_env filter since
        # old version records have NULL application_environment_id.
        ConfigVersion = version_class(Configuration)
        cfg_ids = (
            db.session.query(Configuration.id)
            .filter_by(application_environment_id=app_env.id)
            .union(
                db.session.query(ConfigVersion.id).filter(
                    ConfigVersion.application_id == application.id,
                    ConfigVersion.application_environment_id == app_env.id,
                )
            )
        )
        ing_ids = (
            db.session.query(version_class(Ingress).id)
            .filter_by(application_environment_id=app_env.id)
            .distinct()
        )
        img_ids = db.session.query(Image.id).filter_by(
            application_environment_id=app_env.id
        )
        rel_ids = db.session.query(Release.id).filter_by(
            application_environment_id=app_env.id
        )
        dep_ids = db.session.query(Deployment.id).filter_by(
            application_environment_id=app_env.id
        )

        filters = [
            or_(
                and_(
                    Activity.object_type == "Application", Activity.object_id == app_id
                ),
                and_(
                    Activity.object_type == "ApplicationEnvironment",
                    Activity.object_id == ae_id,
                ),
                and_(
                    Activity.object_type.in_(
                        ["Configuration", "EnvironmentConfiguration"]
                    ),
                    Activity.object_id.in_(cfg_ids),
                ),
                and_(
                    Activity.object_type == "Ingress", Activity.object_id.in_(ing_ids)
                ),
                and_(Activity.object_type == "Image", Activity.object_id.in_(img_ids)),
                and_(
                    Activity.object_type == "Release", Activity.object_id.in_(rel_ids)
                ),
                and_(
                    Activity.object_type == "Deployment",
                    Activity.object_id.in_(dep_ids),
                ),
            )
        ]

    if verb:
        filters.append(Activity.verb == verb)
    if object_type:
        filters.append(Activity.object_type == object_type)

    return (
        db.session.query(Activity)
        .filter(*filters)
        .order_by(Activity.id.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )


# ---------------------------------------------------------------------------
# Audit log diff computation
# ---------------------------------------------------------------------------

_VERSION_MODEL_MAP = {
    "Configuration": Configuration,
    "EnvironmentConfiguration": EnvironmentConfiguration,
    "Image": Image,
    "Release": Release,
    "Deployment": Deployment,
    "Ingress": Ingress,
    "Application": Application,
    "ApplicationEnvironment": ApplicationEnvironment,
    "Organization": Organization,
    "Environment": Environment,
}

_DIFF_FIELDS = {
    "Configuration": [],  # handled specially
    "EnvironmentConfiguration": [],
    "Image": ["build_ref"],
    "Release": [],
    "Deployment": [],
    "Ingress": [
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
    ],
    "Application": [
        "name",
        "slug",
        "github_repository",
        "auto_deploy_branch",
        "github_environment_name",
        "health_check_path",
        "health_check_host",
        "deployment_timeout",
        "privileged",
        "subdirectory",
        "dockerfile_path",
        "platform",
        "github_repository_is_private",
        "process_counts",
        "process_pod_classes",
        "branch_deploy_watch_paths",
    ],
    "ApplicationEnvironment": [
        "process_counts",
        "process_pod_classes",
        "deployment_timeout",
        "health_check_path",
        "health_check_host",
        "auto_deploy_branch",
        "github_environment_name",
        "auto_deploy_wait_for_ci",
    ],
    "Organization": ["name"],
    "Environment": ["name"],
}

_NAME_FIELD = {
    "Configuration": "name",
    "EnvironmentConfiguration": "name",
    "Image": "version",
    "Release": "version",
    "Ingress": "name",
    "Application": "name",
    "Organization": "name",
    "Environment": "name",
}


def _diff_fields(current, previous, fields):
    """Compare fields between two version records, expanding dicts/lists."""
    changes = []
    for field in fields:
        new_val = getattr(current, field, None)
        old_val = getattr(previous, field, None) if previous else None
        if previous is None:
            if new_val is not None and new_val != "":
                changes.append({"type": "add", "text": f"{field} {new_val}"})
        elif old_val != new_val:
            if isinstance(new_val, dict) and isinstance(old_val, dict):
                for k in sorted(set(list(old_val.keys()) + list(new_val.keys()))):
                    ov, nv = old_val.get(k), new_val.get(k)
                    if ov != nv:
                        label = f"{field}.{k}"
                        if ov is None:
                            changes.append({"type": "add", "text": f"{label} {nv}"})
                        elif nv is None:
                            changes.append({"type": "remove", "text": f"{label} {ov}"})
                        else:
                            changes.append({"type": "remove", "text": f"{label} {ov}"})
                            changes.append({"type": "add", "text": f"{label} {nv}"})
            elif isinstance(new_val, list) and isinstance(old_val, list):
                old_str = ", ".join(str(x) for x in old_val) or "(empty)"
                new_str = ", ".join(str(x) for x in new_val) or "(empty)"
                changes.append({"type": "remove", "text": f"{field} {old_str}"})
                changes.append({"type": "add", "text": f"{field} {new_str}"})
            else:
                old_str = str(old_val) if old_val is not None else ""
                new_str = str(new_val) if new_val is not None else ""
                changes.append({"type": "remove", "text": f"{field} {old_str}"})
                changes.append({"type": "add", "text": f"{field} {new_str}"})
    return changes


def _diff_config(current, previous, name):
    """Produce diff lines for a Configuration version record."""
    changes = []
    is_secret = getattr(current, "secret", False)

    if previous is None:
        # Creation
        if is_secret:
            changes.append({"type": "add", "text": f"{name}=••••••••"})
            changes.append({"type": "add", "text": "secret True"})
        else:
            val = getattr(current, "value", "")
            changes.append({"type": "add", "text": f"{name}={val}"})
        if getattr(current, "buildtime", False):
            changes.append({"type": "add", "text": "buildtime True"})
    else:
        # Edit
        old_secret = getattr(previous, "secret", False)
        old_bt = getattr(previous, "buildtime", False)
        new_bt = getattr(current, "buildtime", False)

        if is_secret:
            if getattr(current, "version_id", None) != getattr(
                previous, "version_id", None
            ):
                changes.append({"type": "secret", "text": f"{name} rotated"})
        else:
            old_val = getattr(previous, "value", "")
            new_val = getattr(current, "value", "")
            if old_val != new_val:
                changes.append({"type": "remove", "text": f"{name}={old_val}"})
                changes.append({"type": "add", "text": f"{name}={new_val}"})

        if old_secret != is_secret:
            changes.append({"type": "remove", "text": f"secret {old_secret}"})
            changes.append({"type": "add", "text": f"secret {is_secret}"})
        if old_bt != new_bt:
            changes.append({"type": "remove", "text": f"buildtime {old_bt}"})
            changes.append({"type": "add", "text": f"buildtime {new_bt}"})

    return changes


def _diff_ingress_hosts_paths(tx_id):
    """Produce diff lines for host/path version records in a transaction."""
    changes = []
    IngressHostVersion = version_class(IngressHost)
    IngressPathVersion = version_class(IngressPath)

    for hv in (
        db.session.query(IngressHostVersion).filter_by(transaction_id=tx_id).all()
    ):
        prev = (
            db.session.query(IngressHostVersion)
            .filter_by(id=hv.id, end_transaction_id=tx_id)
            .first()
        )
        if hv.operation_type == 0 or (hv.operation_type == 1 and not prev):
            changes.append({"type": "add", "text": f"host {hv.hostname}"})
        elif hv.operation_type == 2:
            changes.append({"type": "remove", "text": f"host {hv.hostname}"})
        elif prev:
            for hf in ("tls_enabled", "is_auto_generated"):
                ov, nv = getattr(prev, hf, None), getattr(hv, hf, None)
                if ov != nv:
                    changes.append(
                        {"type": "remove", "text": f"{hv.hostname} {hf} {ov}"}
                    )
                    changes.append({"type": "add", "text": f"{hv.hostname} {hf} {nv}"})

    for pv in (
        db.session.query(IngressPathVersion).filter_by(transaction_id=tx_id).all()
    ):
        prev = (
            db.session.query(IngressPathVersion)
            .filter_by(id=pv.id, end_transaction_id=tx_id)
            .first()
        )
        if pv.operation_type == 0 or (pv.operation_type == 1 and not prev):
            changes.append(
                {"type": "add", "text": f"path {pv.path} → {pv.target_process_name}"}
            )
        elif pv.operation_type == 2:
            changes.append(
                {"type": "remove", "text": f"path {pv.path} → {pv.target_process_name}"}
            )
        elif prev:
            for pf in ("path", "path_type", "target_process_name"):
                ov, nv = getattr(prev, pf, None), getattr(pv, pf, None)
                if ov != nv:
                    changes.append({"type": "remove", "text": f"{pf} {ov}"})
                    changes.append({"type": "add", "text": f"{pf} {nv}"})

    return changes


def compute_activity_diffs(activities):
    """Compute field-level diffs for a list of Activity records.

    Batch-loads version records to minimize queries (typically ~10 queries
    total regardless of page size, instead of 2-3 per activity).

    Returns {activity_id: {
        "name": str or None,
        "is_creation": bool,
        "changes": [{"type": "add"|"remove"|"secret", "text": str}]
    }}
    """
    if not activities:
        return {}

    # Group activities by object_type for batch loading
    by_type = {}
    for a in activities:
        by_type.setdefault(a.object_type, []).append(a)

    # Batch-load current + previous version records per type (2 queries per type)
    current_versions = {}  # (object_type, object_id, tx_id) → version record
    previous_versions = {}  # same key → previous version record

    for obj_type, type_activities in by_type.items():
        model_cls = _VERSION_MODEL_MAP.get(obj_type)
        if not model_cls:
            continue
        try:
            VersionCls = version_class(model_cls)
        except Exception:
            continue

        # Batch-load current versions
        tx_pairs = [(a.object_id, a.object_tx_id) for a in type_activities]
        if tx_pairs:
            current_rows = (
                db.session.query(VersionCls)
                .filter(
                    or_(
                        *[
                            and_(VersionCls.id == oid, VersionCls.transaction_id == tid)
                            for oid, tid in tx_pairs
                        ]
                    )
                )
                .all()
            )
            for row in current_rows:
                current_versions[(obj_type, row.id, row.transaction_id)] = row

            # Batch-load previous versions
            prev_rows = (
                db.session.query(VersionCls)
                .filter(
                    or_(
                        *[
                            and_(
                                VersionCls.id == oid,
                                VersionCls.end_transaction_id == tid,
                            )
                            for oid, tid in tx_pairs
                        ]
                    )
                )
                .all()
            )
            for row in prev_rows:
                previous_versions[(obj_type, row.id, row.end_transaction_id)] = row

    # Batch-load ingress host/path versions for all ingress transactions
    ingress_tx_ids = [a.object_tx_id for a in by_type.get("Ingress", [])]
    host_versions_by_tx = {}
    path_versions_by_tx = {}
    host_prev_by_id = {}
    path_prev_by_id = {}

    if ingress_tx_ids:
        IngressHostVersion = version_class(IngressHost)
        IngressPathVersion = version_class(IngressPath)

        all_hvs = (
            db.session.query(IngressHostVersion)
            .filter(IngressHostVersion.transaction_id.in_(ingress_tx_ids))
            .all()
        )
        for hv in all_hvs:
            host_versions_by_tx.setdefault(hv.transaction_id, []).append(hv)

        # Previous host versions
        hv_prev_filters = [
            and_(
                IngressHostVersion.id == hv.id,
                IngressHostVersion.end_transaction_id == hv.transaction_id,
            )
            for hv in all_hvs
        ]
        if hv_prev_filters:
            for row in (
                db.session.query(IngressHostVersion).filter(or_(*hv_prev_filters)).all()
            ):
                host_prev_by_id[(row.id, row.end_transaction_id)] = row

        all_pvs = (
            db.session.query(IngressPathVersion)
            .filter(IngressPathVersion.transaction_id.in_(ingress_tx_ids))
            .all()
        )
        for pv in all_pvs:
            path_versions_by_tx.setdefault(pv.transaction_id, []).append(pv)

        pv_prev_filters = [
            and_(
                IngressPathVersion.id == pv.id,
                IngressPathVersion.end_transaction_id == pv.transaction_id,
            )
            for pv in all_pvs
        ]
        if pv_prev_filters:
            for row in (
                db.session.query(IngressPathVersion).filter(or_(*pv_prev_filters)).all()
            ):
                path_prev_by_id[(row.id, row.end_transaction_id)] = row

    # Batch-load release versions for deployment activities
    deploy_release_ids = set()
    for a in by_type.get("Deployment", []):
        current = current_versions.get((a.object_type, a.object_id, a.object_tx_id))
        if current:
            rel_data = getattr(current, "release", None)
            if isinstance(rel_data, dict) and rel_data.get("id"):
                deploy_release_ids.add(rel_data["id"])
    release_version_map = {}
    if deploy_release_ids:
        for rel in Release.query.filter(Release.id.in_(deploy_release_ids)).all():
            release_version_map[str(rel.id)] = rel.version

    # Now compute diffs using pre-loaded data
    result = {}
    for a in activities:
        current = current_versions.get((a.object_type, a.object_id, a.object_tx_id))
        if not current:
            continue

        previous = previous_versions.get((a.object_type, a.object_id, a.object_tx_id))

        name_field = _NAME_FIELD.get(a.object_type)
        name = getattr(current, name_field, None) if name_field else None

        is_creation = previous is None
        is_config = a.object_type in ("Configuration", "EnvironmentConfiguration")

        if is_config:
            changes = _diff_config(current, previous, name or "")
        else:
            fields = _DIFF_FIELDS.get(a.object_type, [])
            changes = _diff_fields(current, previous, fields)

            if a.object_type == "Ingress":
                # Use pre-loaded host/path versions
                for hv in host_versions_by_tx.get(a.object_tx_id, []):
                    prev_hv = host_prev_by_id.get((hv.id, hv.transaction_id))
                    if hv.operation_type == 0 or (
                        hv.operation_type == 1 and not prev_hv
                    ):
                        changes.append({"type": "add", "text": f"host {hv.hostname}"})
                    elif hv.operation_type == 2:
                        changes.append(
                            {"type": "remove", "text": f"host {hv.hostname}"}
                        )
                    elif prev_hv:
                        for hf in ("tls_enabled", "is_auto_generated"):
                            ov, nv = getattr(prev_hv, hf, None), getattr(hv, hf, None)
                            if ov != nv:
                                changes.append(
                                    {
                                        "type": "remove",
                                        "text": f"{hv.hostname} {hf} {ov}",
                                    }
                                )
                                changes.append(
                                    {"type": "add", "text": f"{hv.hostname} {hf} {nv}"}
                                )

                for pv in path_versions_by_tx.get(a.object_tx_id, []):
                    prev_pv = path_prev_by_id.get((pv.id, pv.transaction_id))
                    if pv.operation_type == 0 or (
                        pv.operation_type == 1 and not prev_pv
                    ):
                        changes.append(
                            {
                                "type": "add",
                                "text": f"path {pv.path} → {pv.target_process_name}",
                            }
                        )
                    elif pv.operation_type == 2:
                        changes.append(
                            {
                                "type": "remove",
                                "text": f"path {pv.path} → {pv.target_process_name}",
                            }
                        )
                    elif prev_pv:
                        for pf in ("path", "path_type", "target_process_name"):
                            ov, nv = getattr(prev_pv, pf, None), getattr(pv, pf, None)
                            if ov != nv:
                                changes.append({"type": "remove", "text": f"{pf} {ov}"})
                                changes.append({"type": "add", "text": f"{pf} {nv}"})

            elif a.object_type == "Deployment":
                rel_data = getattr(current, "release", None)
                if isinstance(rel_data, dict) and rel_data.get("id"):
                    rel_ver = release_version_map.get(str(rel_data["id"]))
                    if rel_ver:
                        changes.append({"type": "add", "text": f"release v{rel_ver}"})

        # Skip no-op edits
        if a.verb == "edit" and not is_creation and not changes:
            continue

        result[a.id] = {
            "name": name,
            "is_creation": is_creation,
            "changes": changes,
        }

    return result


def split_image_processes(image):
    """Split image.processes into (service_procs, release_cmds, postdeploy_cmds, job_procs).

    Mirrors Release.processes / release_commands / postdeploy_commands / job_processes
    without triggering an image_object query.
    """
    if not image or not image.processes:
        return {}, {}, {}, {}
    all_procs = image.processes
    service_procs = {
        k: v
        for k, v in all_procs.items()
        if not (
            k.startswith("release") or k.startswith("postdeploy") or k.startswith("job")
        )
    }
    release_cmds = {k: v for k, v in all_procs.items() if k.startswith("release")}
    postdeploy_cmds = {k: v for k, v in all_procs.items() if k.startswith("postdeploy")}
    job_procs = {k: v for k, v in all_procs.items() if k.startswith("job")}
    return service_procs, release_cmds, postdeploy_cmds, job_procs

