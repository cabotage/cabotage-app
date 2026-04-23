"""Audit log diff computation helpers.

Given a page of AuditLog entries, batch-fetches version records from
sqlalchemy-continuum and computes field-level "what changed" diffs.
"""

import json as _json
from collections import defaultdict

from sqlalchemy_continuum import version_class

from cabotage.server import db
from cabotage.server.models.projects import (
    Application,
    Configuration,
    Ingress,
    IngressHost,
    IngressPath,
    Project,
    Release,
)

# ---------------------------------------------------------------------------
# Field maps: column name → human-readable label
# ---------------------------------------------------------------------------

_APP_DIFF_FIELDS = {
    "auto_deploy_branch": "auto deploy branch",
    "health_check_path": "health check path",
    "health_check_host": "health check host",
    "deployment_timeout": "deployment timeout",
    "privileged": "privileged",
    "github_app_installation_id": "GitHub App",
    "github_repository": "GitHub repository",
    "github_environment_name": "GitHub environment",
    "github_repository_is_private": "private repository",
    "process_counts": "process counts",
    "process_pod_classes": "pod classes",
    "subdirectory": "subdirectory",
    "dockerfile_path": "Dockerfile path",
    "procfile_path": "Procfile path",
    "branch_deploy_watch_paths": "watch paths",
}

_PROJECT_DIFF_FIELDS = {
    "name": "name",
    "environments_enabled": "environments",
    "branch_deploys_enabled": "branch deploys",
}

_INGRESS_DIFF_FIELDS = {
    "enabled": "enabled",
    "ingress_class_name": "ingress class",
    "backend_protocol": "backend protocol",
    "proxy_connect_timeout": "connect timeout",
    "proxy_read_timeout": "read timeout",
    "proxy_send_timeout": "send timeout",
    "proxy_body_size": "body size",
    "client_body_buffer_size": "client buffer",
    "proxy_request_buffering": "request buffering",
    "session_affinity": "session affinity",
    "use_regex": "use regex",
    "cluster_issuer": "certificate issuer",
    "force_ssl_redirect": "force SSL",
    "service_upstream": "service upstream",
    "tailscale_hostname": "Tailscale hostname",
    "tailscale_funnel": "Tailscale Funnel",
    "tailscale_tags": "Tailscale tags",
}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def format_value(val):
    """Format a version field value for display."""
    if val is None:
        return None
    if isinstance(val, bool):
        return "yes" if val else "no"
    if isinstance(val, dict):
        return _json.dumps(val, separators=(",", ":"))
    s = str(val)
    if not s:
        return "(none)"
    return s


def diff_versions(prev, cur, field_map):
    """Compare two version records and return a list of change dicts.

    If prev is None (no previous version found), returns empty — we can't
    meaningfully diff against nothing for edit events.

    Skips fields where both old and new are falsy (None/empty string
    transitions aren't meaningful changes).

    Returns [{'field': label, 'old': str|None, 'new': str|None}, ...]
    """
    if not prev or not cur:
        return []
    changes = []
    for col, label in field_map.items():
        old_val = getattr(prev, col, None)
        new_val = getattr(cur, col, None)
        if old_val != new_val:
            # Skip None ↔ empty string transitions (not meaningful)
            if not old_val and not new_val:
                continue
            changes.append(
                {
                    "field": label,
                    "old": format_value(old_val),
                    "new": format_value(new_val),
                }
            )
    return changes


# ---------------------------------------------------------------------------
# Batch version diff fetcher
# ---------------------------------------------------------------------------


def _batch_fetch_version_diffs(entries, model_cls, field_map):
    """Batch-fetch before/after versions for a list of audit entries.

    Returns {entry_id: [change_dicts]}
    """
    if not entries:
        return {}

    ver_cls = version_class(model_cls)
    tx_ids = [e.object_tx_id for e in entries if e.object_tx_id]
    obj_ids = [e.object_id for e in entries if e.object_tx_id]

    if not tx_ids:
        return {}

    # Fetch current versions (the "after" state)
    cur_versions = (
        db.session.query(ver_cls)
        .filter(
            ver_cls.id.in_(obj_ids),
            ver_cls.transaction_id.in_(tx_ids),
        )
        .all()
    )
    cur_by_key = {(v.id, v.transaction_id): v for v in cur_versions}

    # Fetch previous versions (the "before" state)
    prev_versions = (
        db.session.query(ver_cls)
        .filter(
            ver_cls.id.in_(obj_ids),
            ver_cls.end_transaction_id.in_(tx_ids),
        )
        .all()
    )
    prev_by_key = {(v.id, v.end_transaction_id): v for v in prev_versions}

    result = {}
    for e in entries:
        if not e.object_tx_id:
            continue
        key = (e.object_id, e.object_tx_id)
        cur = cur_by_key.get(key)
        prev = prev_by_key.get(key)
        if cur:
            changes = diff_versions(prev, cur, field_map)
            if changes:
                result[e.id] = changes
    return result


# ---------------------------------------------------------------------------
# Per-type change computation
# ---------------------------------------------------------------------------


def _compute_scale_changes(entries):
    """Extract scale changes from raw_data for Application scale events."""
    result = {}
    for e in entries:
        raw = e.raw_data or {}
        changes_data = raw.get("changes", {})
        if not changes_data:
            continue
        changes = []
        for proc_name, proc_changes in sorted(changes_data.items()):
            pc = proc_changes.get("process_count", {})
            old_count = pc.get("old_value")
            new_count = pc.get("new_value")
            if old_count != new_count:
                changes.append(
                    {
                        "field": proc_name,
                        "old": str(old_count) if old_count is not None else "0",
                        "new": str(new_count) if new_count is not None else "0",
                    }
                )
            pod = proc_changes.get("pod_class", {})
            old_pod = pod.get("old_value")
            new_pod = pod.get("new_value")
            if old_pod != new_pod and old_pod and new_pod:
                changes.append(
                    {
                        "field": f"{proc_name} pod class",
                        "old": str(old_pod),
                        "new": str(new_pod),
                    }
                )
        if changes:
            result[e.id] = changes
    return result


def _compute_config_changes(entries):
    """Compute changes for Configuration create/edit events from version table."""
    if not entries:
        return {}

    ver_cls = version_class(Configuration)
    tx_ids = [e.object_tx_id for e in entries if e.object_tx_id]
    obj_ids = [e.object_id for e in entries if e.object_tx_id]

    if not tx_ids:
        return {}

    cur_versions = (
        db.session.query(ver_cls)
        .filter(ver_cls.id.in_(obj_ids), ver_cls.transaction_id.in_(tx_ids))
        .all()
    )
    cur_by_key = {(v.id, v.transaction_id): v for v in cur_versions}

    prev_versions = (
        db.session.query(ver_cls)
        .filter(ver_cls.id.in_(obj_ids), ver_cls.end_transaction_id.in_(tx_ids))
        .all()
    )
    prev_by_key = {(v.id, v.end_transaction_id): v for v in prev_versions}

    result = {}
    for e in entries:
        if not e.object_tx_id:
            continue
        key = (e.object_id, e.object_tx_id)
        cur = cur_by_key.get(key)
        prev = prev_by_key.get(key)
        if not cur:
            continue

        changes = []
        is_secret = cur.secret

        # Secrets don't get diffs — just a verb label handled in the template
        if is_secret:
            continue

        name = cur.name

        if e.verb == "delete":
            val = cur.value if cur.value else "(empty)"
            if len(val) > 60:
                val = val[:57] + "..."
            changes.append({"field": name, "old": val, "new": None})
        elif e.verb == "create":
            if cur.value:
                val = cur.value
                if len(val) > 60:
                    val = val[:57] + "..."
                changes.append({"field": name, "old": None, "new": val})
        elif prev:
            old_v = prev.value if prev.value else "(empty)"
            new_v = cur.value if cur.value else "(empty)"
            if len(old_v) > 60:
                old_v = old_v[:57] + "..."
            if len(new_v) > 60:
                new_v = new_v[:57] + "..."
            changes.append({"field": name, "old": old_v, "new": new_v})

        # Flag changes
        if prev and prev.secret != cur.secret:
            changes.append(
                {
                    "field": "secret",
                    "old": "yes" if prev.secret else "no",
                    "new": "yes" if cur.secret else "no",
                }
            )
        if prev and prev.buildtime != cur.buildtime:
            changes.append(
                {
                    "field": "buildtime",
                    "old": "yes" if prev.buildtime else "no",
                    "new": "yes" if cur.buildtime else "no",
                }
            )

        if changes:
            result[e.id] = changes
    return result


def _compute_ingress_changes(entries):
    """Compute changes for Ingress edit events from version tables."""
    if not entries:
        return {}

    host_ver_cls = version_class(IngressHost)
    path_ver_cls = version_class(IngressPath)

    tx_ids = [e.object_tx_id for e in entries if e.object_tx_id]
    obj_ids = [e.object_id for e in entries if e.object_tx_id]

    if not tx_ids:
        return {}

    # Top-level ingress diffs
    setting_changes = _batch_fetch_version_diffs(entries, Ingress, _INGRESS_DIFF_FIELDS)

    # Host changes: find hosts created or ended at these transactions
    hosts_at = (
        db.session.query(host_ver_cls)
        .filter(
            host_ver_cls.ingress_id.in_(obj_ids),
            host_ver_cls.transaction_id.in_(tx_ids),
        )
        .all()
    )
    hosts_ended = (
        db.session.query(host_ver_cls)
        .filter(
            host_ver_cls.ingress_id.in_(obj_ids),
            host_ver_cls.end_transaction_id.in_(tx_ids),
        )
        .all()
    )

    # Group by (ingress_id, tx_id)
    hosts_at_by_ing_tx = defaultdict(list)
    for h in hosts_at:
        hosts_at_by_ing_tx[(h.ingress_id, h.transaction_id)].append(h)
    hosts_ended_by_ing_tx = defaultdict(list)
    for h in hosts_ended:
        hosts_ended_by_ing_tx[(h.ingress_id, h.end_transaction_id)].append(h)

    # Path changes
    paths_at = (
        db.session.query(path_ver_cls)
        .filter(
            path_ver_cls.ingress_id.in_(obj_ids),
            path_ver_cls.transaction_id.in_(tx_ids),
        )
        .all()
    )
    paths_ended = (
        db.session.query(path_ver_cls)
        .filter(
            path_ver_cls.ingress_id.in_(obj_ids),
            path_ver_cls.end_transaction_id.in_(tx_ids),
        )
        .all()
    )

    paths_at_by_ing_tx = defaultdict(list)
    for p in paths_at:
        paths_at_by_ing_tx[(p.ingress_id, p.transaction_id)].append(p)
    paths_ended_by_ing_tx = defaultdict(list)
    for p in paths_ended:
        paths_ended_by_ing_tx[(p.ingress_id, p.end_transaction_id)].append(p)

    result = {}
    for e in entries:
        if not e.object_tx_id:
            continue
        changes = list(setting_changes.get(e.id, []))
        key = (e.object_id, e.object_tx_id)

        # Hosts: 0=INSERT, 1=UPDATE, 2=DELETE
        cur_hosts = hosts_at_by_ing_tx.get(key, [])
        ended_hosts = hosts_ended_by_ing_tx.get(key, [])
        new_host_names = {h.hostname for h in cur_hosts}
        added_hosts = sorted(h.hostname for h in cur_hosts if h.operation_type == 0)
        modified_hosts = sorted(h.hostname for h in cur_hosts if h.operation_type == 1)
        removed_hosts = sorted(
            h.hostname for h in ended_hosts if h.hostname not in new_host_names
        )
        if added_hosts:
            changes.append(
                {"field": "hosts added", "old": None, "new": ", ".join(added_hosts)}
            )
        if modified_hosts:
            changes.append(
                {
                    "field": "hosts changed",
                    "old": None,
                    "new": ", ".join(modified_hosts),
                }
            )
        if removed_hosts:
            changes.append(
                {"field": "hosts removed", "old": ", ".join(removed_hosts), "new": None}
            )

        # Paths: 0=INSERT, 1=UPDATE, 2=DELETE
        cur_paths = paths_at_by_ing_tx.get(key, [])
        ended_paths = paths_ended_by_ing_tx.get(key, [])
        new_path_vals = {p.path for p in cur_paths}
        added_paths = sorted(
            f"{p.path} \u2192 {p.target_process_name}"
            for p in cur_paths
            if p.operation_type == 0
        )
        modified_paths = sorted(p.path for p in cur_paths if p.operation_type == 1)
        removed_paths = sorted(
            p.path for p in ended_paths if p.path not in new_path_vals
        )
        if added_paths:
            changes.append(
                {"field": "paths added", "old": None, "new": ", ".join(added_paths)}
            )
        if modified_paths:
            changes.append(
                {
                    "field": "paths changed",
                    "old": None,
                    "new": ", ".join(modified_paths),
                }
            )
        if removed_paths:
            changes.append(
                {"field": "paths removed", "old": ", ".join(removed_paths), "new": None}
            )

        if changes:
            result[e.id] = changes
    return result


def _verify_config_changes(rel, cfg_changed):
    """Filter out false-positive config 'changed' entries.

    The Release model sometimes marks all configs as changed (e.g. when
    version_ids are regenerated) even though the actual values are identical.
    Cross-check against the previous release's snapshot.
    """
    if not cfg_changed:
        return []
    # Find the previous release in the same app/env
    prev_rel = (
        Release.query.filter(
            Release.application_id == rel.application_id,
            Release.application_environment_id == rel.application_environment_id,
            Release.version < rel.version,
        )
        .order_by(Release.version.desc())
        .first()
    )
    if not prev_rel or not prev_rel.configuration:
        return cfg_changed

    prev_cfg = prev_rel.configuration
    cur_cfg = rel.configuration or {}
    truly_changed = []
    for name in cfg_changed:
        old = prev_cfg.get(name, {})
        new = cur_cfg.get(name, {})
        # Compare actual content, normalizing missing keys to defaults
        if (
            old.get("value") != new.get("value")
            or old.get("secret", False) != new.get("secret", False)
            or old.get("buildtime", False) != new.get("buildtime", False)
        ):
            truly_changed.append(name)
    return truly_changed


def _compute_release_changes(entries):
    """Compute changes for Release create events from the release's own change fields."""
    if not entries:
        return {}

    release_ids = [e.object_id for e in entries]
    releases = Release.query.filter(Release.id.in_(release_ids)).all()
    release_by_id = {r.id: r for r in releases}

    result = {}
    for e in entries:
        rel = release_by_id.get(e.object_id)
        if not rel:
            continue

        changes = []
        img_ch = rel.image_changes or {}
        cfg_ch = rel.configuration_changes or {}
        ing_ch = rel.ingress_changes or {}
        img_snap = rel.image or {}

        # Image: show the actual tag/commit, not just which fields changed
        img_changed = img_ch.get("changed", [])
        img_added = img_ch.get("added", [])
        if img_changed or img_added:
            tag = img_snap.get("tag")
            sha = img_snap.get("commit_sha")
            if sha:
                desc = sha[:7]
                if tag:
                    desc = f"#{tag} ({sha[:7]})"
            elif tag:
                desc = f"#{tag}"
            else:
                desc = "new image"
            changes.append({"field": "image", "old": None, "new": desc})

        # Config: list actual names — verify "changed" entries are real
        cfg_added = cfg_ch.get("added", [])
        cfg_changed = _verify_config_changes(rel, cfg_ch.get("changed", []))
        cfg_removed = cfg_ch.get("removed", [])
        for label, items, is_removal in [
            ("config added", cfg_added, False),
            ("config changed", cfg_changed, False),
            ("config removed", cfg_removed, True),
        ]:
            if not items:
                continue
            if len(items) > 10:
                # Bulk change (e.g. environment migration) — just show count
                desc = f"{len(items)} variables"
            elif len(items) > 5:
                limit = 3
                desc = ", ".join(items[:limit]) + f" (+{len(items) - limit} more)"
            else:
                desc = ", ".join(items)
            if is_removal:
                changes.append({"field": label, "old": desc, "new": None})
            else:
                changes.append({"field": label, "old": None, "new": desc})

        # Ingress: list names
        ing_added = ing_ch.get("added", [])
        ing_changed = ing_ch.get("changed", [])
        ing_removed = ing_ch.get("removed", [])
        if ing_added:
            changes.append(
                {"field": "ingress added", "old": None, "new": ", ".join(ing_added)}
            )
        if ing_changed:
            changes.append(
                {"field": "ingress changed", "old": None, "new": ", ".join(ing_changed)}
            )
        if ing_removed:
            changes.append(
                {"field": "ingress removed", "old": ", ".join(ing_removed), "new": None}
            )

        if changes:
            result[e.id] = changes
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_audit_changes(entries):
    """Compute what-changed details for a page of audit log entries.

    Batch-fetches version records grouped by object_type to avoid N+1 queries.

    Returns {entry_id: [{'field': str, 'old': str|None, 'new': str|None}, ...]}
    """
    if not entries:
        return {}

    # Group entries by (object_type, verb)
    groups = defaultdict(list)
    for e in entries:
        groups[(e.object_type, e.verb)].append(e)

    result = {}

    # Application edits → version table diff
    result.update(
        _batch_fetch_version_diffs(
            groups.get(("Application", "edit"), []), Application, _APP_DIFF_FIELDS
        )
    )

    # Application scale → raw_data extraction
    result.update(_compute_scale_changes(groups.get(("Application", "scale"), [])))

    # Configuration creates/edits/deletes → version table diff (secret-aware)
    config_entries = (
        groups.get(("Configuration", "create"), [])
        + groups.get(("Configuration", "edit"), [])
        + groups.get(("Configuration", "delete"), [])
    )
    result.update(_compute_config_changes(config_entries))

    # Ingress edits → version table + host/path version diffs
    result.update(_compute_ingress_changes(groups.get(("Ingress", "edit"), [])))

    # Project edits → version table diff
    result.update(
        _batch_fetch_version_diffs(
            groups.get(("Project", "edit"), []), Project, _PROJECT_DIFF_FIELDS
        )
    )

    # Release creates → release model's own change tracking fields
    release_entries = groups.get(("Release", "create"), []) + groups.get(
        ("Release", "edit"), []
    )
    result.update(_compute_release_changes(release_entries))

    return result
