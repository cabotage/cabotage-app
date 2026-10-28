"""Hourly cost collector.

``collect_hour(period_start)`` queries Mimir (pod cpu/memory usage and
requests, pod labels, traefik response bytes) and the Kubernetes API (node
inventory), classifies every pod into an organization/project/environment
/application/resource scope, prices it at the rates in
``cabotage.server.applets.cost.rates``, and writes one ``CostCollectionRun`` plus a
set of ``CostLineItem`` rows for that UTC hour. Collection is idempotent:
re-running for an hour that was already collected replaces its rows.
"""

import datetime
import hashlib
import re
import struct
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

import kubernetes.client
import sqlalchemy as sa
from flask import current_app
from kubernetes.client.exceptions import ApiException
from sqlalchemy.orm import joinedload, selectinload

from cabotage.server import db
from cabotage.server import kubernetes as kubernetes_ext
from cabotage.server.applets.cost.constants import (
    BUILD_POD_RE,
    COMPONENT_MAX_LEN,
    CPU_REQUEST_QUERY,
    CPU_USAGE_QUERY,
    GIB,
    HOURS_PER_MONTH,
    MEM_REQUEST_QUERY,
    MEM_USAGE_QUERY,
    NETWORK_RECEIVE_QUERY,
    NETWORK_TRANSMIT_QUERY,
    POD_LABELS_QUERY,
    STEP_HOURS,
    STEP_SECONDS,
    STEPS_PER_HOUR,
    TRAEFIK_QUERY,
)
from cabotage.server.applets.cost.helpers import (
    floor_hour,
    parse_cpu_quantity,
    parse_memory_quantity_gib,
)
from cabotage.server.applets.cost.rates import (
    effective_rates,
    node_hourly_price,
    volume_monthly_cost,
)
from cabotage.server.applets.cost.types import (
    CostRates,
    GroupKey,
    PlatformComponentStats,
    PodClassification,
    PodKey,
    ScopeKey,
)
from cabotage.server.models.auth import Organization
from cabotage.server.models.cost import (
    CostCollectionRun,
    CostLineItem,
    CostNode,
    CostNodeHour,
    CostRunStats,
)
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Environment,
    Image,
    Ingress,
    Project,
    Release,
)
from cabotage.server.models.resources import PostgresResource, RedisResource, Resource
from cabotage.server.models.utils import safe_k8s_name
from cabotage.utils import mimir
from cabotage.utils.mimir import MimirSeries


def collect_hour(period_start: datetime.datetime) -> CostCollectionRun:
    """Collect and price one UTC hour of usage, writing a CostCollectionRun.

    Idempotent: replaces any existing run/line-items for the same hour. A
    collection failure (including a Mimir response with no pod metrics at
    all, which almost always means an outage or retention gap rather than a
    genuinely idle whole-install hour) never overwrites a previously
    succeeded run with empty data -- the prior run and its line items are
    left untouched so a transient blip can't wipe good cost history.

    The returned run's own ``status``/``error`` always describe the data it
    currently holds (which may be older, preserved data), not necessarily
    what *this* attempt did. Callers that need to distinguish "collected
    fresh just now" from "this attempt failed, previous data kept" must
    check ``run.stats["last_attempt_status"]``/``["last_attempt_error"]``/
    ``["last_attempt_at"]``, which are always updated to reflect this
    specific attempt regardless of which branch handled it.
    """
    period_start = floor_hour(period_start)
    period_end = period_start + datetime.timedelta(hours=1)
    started_at = datetime.datetime.now(datetime.UTC)
    rates = effective_rates()

    try:
        line_items, node_hours, stats = _collect(period_start, period_end, rates)
    except Exception as exc:
        existing: CostCollectionRun | None = CostCollectionRun.query.filter_by(
            period_start=period_start
        ).first()
        if existing is not None and existing.status == "succeeded":
            return _record_attempt(existing, status="failed", error=str(exc))
        return _write_run(
            period_start,
            started_at,
            rates,
            status="failed",
            error=str(exc),
            stats={},
            line_items=[],
            node_hours=[],
        )

    return _write_run(
        period_start,
        started_at,
        rates,
        status="succeeded",
        error=None,
        stats=stats,
        line_items=line_items,
        node_hours=node_hours,
    )


def _record_attempt(
    run: CostCollectionRun, *, status: str, error: str | None
) -> CostCollectionRun:
    """Stamp this attempt's outcome onto an existing (preserved) run's
    stats, without touching its line items or any other stats fields, so
    freshness reporting can see that collection is currently failing even
    though the run's own `status`/data are untouched.
    """
    attempt_at = datetime.datetime.now(datetime.UTC)
    run.stats = {
        **(run.stats or {}),
        "last_attempt_status": status,
        "last_attempt_error": error,
        "last_attempt_at": attempt_at.isoformat(),
    }
    db.session.add(run)
    db.session.commit()
    return run


def _write_run(
    period_start: datetime.datetime,
    started_at: datetime.datetime,
    rates: CostRates,
    *,
    status: str,
    error: str | None,
    stats: CostRunStats,
    line_items: list[CostLineItem],
    node_hours: list[CostNodeHour],
) -> CostCollectionRun:
    # Beat and "Collect now" can both rewrite the same hour; serialize them so
    # the second waits instead of losing its insert to the unique constraint.
    lock_hash = hashlib.sha256(
        f"cost_collection_run:{int(period_start.timestamp())}".encode()
    ).digest()
    lock_key = struct.unpack(">q", lock_hash[:8])[0]
    _ = db.session.execute(
        sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
    )

    # Keep AWS true-up rows; they belong to a day, not this run.
    CostLineItem.query.filter(
        CostLineItem.period_start == period_start, CostLineItem.run_id.isnot(None)
    ).delete()
    CostNodeHour.query.filter_by(period_start=period_start).delete()
    CostCollectionRun.query.filter_by(period_start=period_start).delete()
    db.session.flush()

    finished_at = datetime.datetime.now(datetime.UTC)
    full_stats: CostRunStats = {
        **stats,
        "last_attempt_status": status,
        "last_attempt_error": error,
        "last_attempt_at": finished_at.isoformat(),
    }

    run = CostCollectionRun(
        period_start=period_start,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        error=error,
        rates=rates,
        stats=full_stats,
    )
    db.session.add(run)
    db.session.flush()

    for item in line_items:
        item.run_id = run.id
        item.period_start = period_start
        db.session.add(item)
    for node_hour in node_hours:
        node_hour.run_id = run.id
        node_hour.period_start = period_start
        db.session.add(node_hour)
    db.session.commit()
    return run


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _collect(
    period_start: datetime.datetime, period_end: datetime.datetime, rates: CostRates
) -> tuple[list[CostLineItem], list[CostNodeHour], CostRunStats]:
    # TODO: Include anonymous until migration to multi-tenant is completed
    # (mirrors _mimir_connection in cabotage.server.user.views).
    base_tenant = current_app.config.get("MIMIR_TENANT_ID")
    tenant_id = f"{base_tenant}|anonymous" if base_tenant else "anonymous"
    range_start = int(period_start.timestamp())
    range_end = int((period_start + datetime.timedelta(minutes=55)).timestamp())
    instant_time = int(period_end.timestamp())

    cpu_usage_result = mimir.query_range(
        CPU_USAGE_QUERY, range_start, range_end, STEP_SECONDS, tenant_id=tenant_id
    )
    mem_usage_result = mimir.query_range(
        MEM_USAGE_QUERY, range_start, range_end, STEP_SECONDS, tenant_id=tenant_id
    )
    cpu_request_result = mimir.query_range(
        CPU_REQUEST_QUERY, range_start, range_end, STEP_SECONDS, tenant_id=tenant_id
    )
    mem_request_result = mimir.query_range(
        MEM_REQUEST_QUERY, range_start, range_end, STEP_SECONDS, tenant_id=tenant_id
    )
    cpu_usage = _series_by_pod(cpu_usage_result)
    mem_usage = _series_by_pod_scaled(mem_usage_result, 1.0 / GIB)
    cpu_request = _series_by_pod(cpu_request_result)
    mem_request = _series_by_pod_scaled(mem_request_result, 1.0 / GIB)
    # kube-state-metrics names a pod's node authoritatively, so requests go
    # first; cAdvisor's node label covers best-effort pods with no requests.
    node_by_pod_step = _node_by_pod_step(
        cpu_request_result, mem_request_result, cpu_usage_result, mem_usage_result
    )
    pod_labels_result = mimir.query_instant(
        POD_LABELS_QUERY, tenant_id=tenant_id, time=instant_time
    )
    traefik_result = mimir.query_instant(
        TRAEFIK_QUERY, tenant_id=tenant_id, time=instant_time
    )
    network_receive_result = mimir.query_instant(
        NETWORK_RECEIVE_QUERY, tenant_id=tenant_id, time=instant_time
    )
    network_transmit_result = mimir.query_instant(
        NETWORK_TRANSMIT_QUERY, tenant_id=tenant_id, time=instant_time
    )

    pod_labels: dict[PodKey, dict[str, str]] = {}
    for series in pod_labels_result:
        metric = series.get("metric", {})
        pod_labels[(metric.get("namespace"), metric.get("pod"))] = metric

    network_bytes_by_pod: defaultdict[PodKey, float] = defaultdict(float)
    for series in list(network_receive_result or []) + list(
        network_transmit_result or []
    ):
        metric = series.get("metric", {})
        pod_key: PodKey = (metric.get("namespace"), metric.get("pod"))
        if not pod_key[0] or not pod_key[1]:
            continue
        value = series.get("value")
        try:
            network_bytes_by_pod[pod_key] += float(value[1]) if value else 0.0
        except TypeError, ValueError, IndexError:
            pass
    network_gb_total = sum(network_bytes_by_pod.values()) / 1e9

    build_job_ids: set[str] = set()
    for (_, pod_name), labels in pod_labels.items():
        if pod_name and labels.get("label_process") == "build":
            match = BUILD_POD_RE.match(pod_name)
            if match:
                build_job_ids.add(match.group(1))
    build_environment_by_job = _resolve_build_app_envs(build_job_ids)

    caches = _build_scope_caches()

    pods_seen = 0
    pods_unallocated = 0

    group_compute: defaultdict[GroupKey, dict[str, float]] = defaultdict(
        lambda: {
            "cpu_billed": 0.0,
            "cpu_usage": 0.0,
            "cpu_request": 0.0,
            "mem_billed": 0.0,
            "mem_usage": 0.0,
            "mem_request": 0.0,
            "network_gb": 0.0,
        }
    )
    platform_components: defaultdict[str, PlatformComponentStats] = defaultdict(
        _empty_platform_stats
    )

    node_load: defaultdict[str, _NodeLoad] = defaultdict(_NodeLoad)

    all_pod_keys: set[PodKey] = (
        set(cpu_usage) | set(mem_usage) | set(cpu_request) | set(mem_request)
    )
    if not all_pod_keys:
        raise RuntimeError("no pod metrics for hour")
    # Labels come from kube-state-metrics, usage from cAdvisor; a KSM gap
    # would otherwise record a "succeeded" hour with every pod unallocated.
    if not pod_labels:
        raise RuntimeError("no pod labels for hour")
    # Pods seen only by the network query still get billed for their bytes.
    all_pod_keys |= set(network_bytes_by_pod)

    for pod_key in all_pod_keys:
        namespace, pod_name = pod_key
        if not namespace or not pod_name:
            continue
        pods_seen += 1
        # Every pod occupies its node, attributed or not, so node load is
        # tallied before classification can skip it.
        _add_node_load(
            node_load,
            node_by_pod_step.get(pod_key, {}),
            cpu_usage.get(pod_key, {}),
            cpu_request.get(pod_key, {}),
            mem_usage.get(pod_key, {}),
            mem_request.get(pod_key, {}),
        )
        network_gb = network_bytes_by_pod.get(pod_key, 0.0) / 1e9
        labels = pod_labels.get(pod_key)
        classification = (
            _classify_pod(namespace, pod_name, labels, caches, build_environment_by_job)
            if labels is not None
            else None
        )
        if classification is None:
            if _claims_tenant(namespace, labels, caches):
                pods_unallocated += 1
                continue
            # No tenant labels: platform, even in a namespace a tenant shares.
            cpu_b, _, _ = _billed_hours(
                cpu_usage.get(pod_key, {}), cpu_request.get(pod_key, {})
            )
            mem_b, _, _ = _billed_hours(
                mem_usage.get(pod_key, {}), mem_request.get(pod_key, {})
            )
            cost = (
                cpu_b * rates["vcpu_hour"]
                + mem_b * rates["memory_gib_hour"]
                + network_gb * rates["network_gb"]
            )
            name = f"{namespace}/{_platform_pod_workload(pod_name)}"
            _add_platform(
                platform_components,
                name,
                cpu_billed_hours=cpu_b,
                memory_billed_gib_hours=mem_b,
                network_gb=network_gb,
                cost_usd=cost,
            )
            continue

        cpu_b, cpu_u, cpu_r = _billed_hours(
            cpu_usage.get(pod_key, {}), cpu_request.get(pod_key, {})
        )
        mem_b, mem_u, mem_r = _billed_hours(
            mem_usage.get(pod_key, {}), mem_request.get(pod_key, {})
        )

        key: GroupKey = (
            classification["category"],
            classification["organization_id"],
            classification["project_id"],
            classification["environment_id"],
            classification["application_id"],
            classification["resource_id"],
            classification["component"],
        )
        g = group_compute[key]
        g["cpu_billed"] += cpu_b
        g["cpu_usage"] += cpu_u
        g["cpu_request"] += cpu_r
        g["mem_billed"] += mem_b
        g["mem_usage"] += mem_u
        g["mem_request"] += mem_r
        g["network_gb"] += network_gb

    line_items: list[CostLineItem] = []
    direct_compute_total = 0.0
    direct_weight_by_scope: defaultdict[ScopeKey, float] = defaultdict(float)

    for key, totals in group_compute.items():
        category, org_id, project_id, env_id, app_id, resource_id, component = key
        scope_key: ScopeKey = (org_id, project_id, env_id, app_id, resource_id)

        if (
            abs(totals["cpu_billed"]) > 1e-9
            or totals["cpu_usage"]
            or totals["cpu_request"]
        ):
            cost = totals["cpu_billed"] * rates["vcpu_hour"]
            line_items.append(
                CostLineItem(
                    organization_id=org_id,
                    project_id=project_id,
                    environment_id=env_id,
                    application_id=app_id,
                    resource_id=resource_id,
                    category=category,
                    meter="vcpu",
                    component=component,
                    quantity=totals["cpu_billed"],
                    unit="vCPU-hr",
                    usage_quantity=totals["cpu_usage"],
                    request_quantity=totals["cpu_request"],
                    cost_usd=cost,
                )
            )
            if category in ("compute", "backing_service", "build"):
                direct_compute_total += cost
                direct_weight_by_scope[scope_key] += cost

        if (
            abs(totals["mem_billed"]) > 1e-9
            or totals["mem_usage"]
            or totals["mem_request"]
        ):
            cost = totals["mem_billed"] * rates["memory_gib_hour"]
            line_items.append(
                CostLineItem(
                    organization_id=org_id,
                    project_id=project_id,
                    environment_id=env_id,
                    application_id=app_id,
                    resource_id=resource_id,
                    category=category,
                    meter="memory",
                    component=component,
                    quantity=totals["mem_billed"],
                    unit="GiB-hr",
                    usage_quantity=totals["mem_usage"],
                    request_quantity=totals["mem_request"],
                    cost_usd=cost,
                )
            )
            if category in ("compute", "backing_service", "build"):
                direct_compute_total += cost
                direct_weight_by_scope[scope_key] += cost

        # Kept out of the overhead weights: network isn't compute capacity.
        if totals["network_gb"] > 1e-12:
            cost = totals["network_gb"] * rates["network_gb"]
            line_items.append(
                CostLineItem(
                    organization_id=org_id,
                    project_id=project_id,
                    environment_id=env_id,
                    application_id=app_id,
                    resource_id=resource_id,
                    category="network",
                    meter="network",
                    component=component,
                    quantity=totals["network_gb"],
                    unit="GB",
                    usage_quantity=None,
                    request_quantity=None,
                    cost_usd=cost,
                )
            )

    # --- Storage -----------------------------------------------------
    storage_items = _collect_storage(period_start, period_end, rates)
    line_items.extend(storage_items)

    pv_items, storage_inventory_error = _collect_pv_storage(
        period_end, rates, caches, platform_components
    )
    line_items.extend(pv_items)

    # --- Egress --------------------------------------------------------
    egress_items = _collect_egress(traefik_result, rates)
    line_items.extend(egress_items)

    # --- Overhead --------------------------------------------------------
    node_hours, node_inventory_error = _collect_nodes(node_load, rates)
    cluster_compute_cost = sum(
        float(row.cost_usd) for row in node_hours if row.cost_usd is not None
    )
    fixed_overhead = sum(rates.get("fixed_hourly", {}).values())
    overhead_pool = (
        max(cluster_compute_cost - direct_compute_total, 0.0) + fixed_overhead
    )

    if direct_weight_by_scope and sum(direct_weight_by_scope.values()) > 0:
        total_weight = sum(direct_weight_by_scope.values())
        for scope_key, weight in direct_weight_by_scope.items():
            org_id, project_id, env_id, app_id, resource_id = scope_key
            share = weight / total_weight
            cost = overhead_pool * share
            line_items.append(
                CostLineItem(
                    organization_id=org_id,
                    project_id=project_id,
                    environment_id=env_id,
                    application_id=app_id,
                    resource_id=resource_id,
                    category="overhead",
                    meter="shared",
                    component="shared",
                    quantity=cost,
                    unit="USD",
                    usage_quantity=None,
                    request_quantity=None,
                    cost_usd=cost,
                )
            )
        overhead_allocated = overhead_pool
    else:
        overhead_allocated = 0.0

    platform_cost = sum(entry["cost_usd"] for entry in platform_components.values())

    stats: CostRunStats = {
        "pods_seen": pods_seen,
        "pods_unallocated": pods_unallocated,
        "node_hours": sum(row.node_hours for row in node_hours),
        "nodes_unpriced": sum(1 for row in node_hours if row.cost_usd is None),
        "node_data_available": bool(node_hours),
        "node_inventory_error": node_inventory_error,
        "cluster_compute_cost": cluster_compute_cost,
        "tenant_compute_cost": direct_compute_total,
        "overhead_pool": overhead_pool,
        "overhead_allocated": overhead_allocated,
        "fixed_overhead": fixed_overhead,
        "network_gb_total": network_gb_total,
        "platform_components": dict(platform_components),
        "platform_cost": platform_cost,
        "storage_inventory_error": storage_inventory_error,
    }

    return line_items, node_hours, stats


# ---------------------------------------------------------------------------
# Mimir series helpers
# ---------------------------------------------------------------------------


def _series_by_pod(
    result: list[MimirSeries] | None,
) -> dict[PodKey, dict[float, float]]:
    """Map a Mimir range-query result (summed by namespace,pod,node) to
    ``{(namespace, pod): {timestamp: value}}``.

    A pod splits into several series when its node label changes (a
    fixed-name pod rescheduled mid-hour, or cAdvisor series from before and
    after Alloy started labelling them). Both describe the same pod, so a
    step present in more than one keeps the larger value instead of adding.
    """
    out: dict[PodKey, dict[float, float]] = {}
    for series in result or []:
        metric = series.get("metric", {})
        key = (metric.get("namespace"), metric.get("pod"))
        points = out.setdefault(key, {})
        for ts, val in series.get("values", []):
            try:
                ts_f, val_f = float(ts), float(val)
            except TypeError, ValueError:
                continue
            points[ts_f] = max(points.get(ts_f, val_f), val_f)
    return out


def _series_by_pod_scaled(
    result: list[MimirSeries] | None, scale: float
) -> dict[PodKey, dict[float, float]]:
    out = _series_by_pod(result)
    return {
        key: {ts: v * scale for ts, v in points.items()} for key, points in out.items()
    }


def _billed_hours(
    usage_points: dict[float, float], request_points: dict[float, float]
) -> tuple[float, float, float]:
    """Return (billed_hr, usage_hr, request_hr) summed across all steps,
    where billed = max(usage, request) and each step counts for 5/60 hours.
    """
    timestamps = set(usage_points) | set(request_points)
    billed = 0.0
    usage_sum = 0.0
    request_sum = 0.0
    for ts in timestamps:
        u = usage_points.get(ts, 0.0)
        r = request_points.get(ts, 0.0)
        billed += max(u, r) * STEP_HOURS
        usage_sum += u * STEP_HOURS
        request_sum += r * STEP_HOURS
    return billed, usage_sum, request_sum


def _node_by_pod_step(
    *results: list[MimirSeries] | None,
) -> dict[PodKey, dict[float, str]]:
    """``{(namespace, pod): {timestamp: node}}`` from each series' ``node``
    label; earlier results win, so pass the most authoritative first."""
    out: dict[PodKey, dict[float, str]] = {}
    for result in results:
        for series in result or []:
            metric = series.get("metric", {})
            node = metric.get("node")
            if not node:
                continue
            steps = out.setdefault((metric.get("namespace"), metric.get("pod")), {})
            for ts, _ in series.get("values", []):
                try:
                    _ = steps.setdefault(float(ts), node)
                except TypeError, ValueError:
                    continue
    return out


@dataclass(slots=True)
class _NodeLoad:
    """One node's pod load over an hour, in vCPU-hr / GiB-hr."""

    steps: set[float] = field(default_factory=set)
    cpu_usage: float = 0.0
    cpu_request: float = 0.0
    cpu_billed: float = 0.0
    mem_usage: float = 0.0
    mem_request: float = 0.0
    mem_billed: float = 0.0


def _add_node_load(
    node_load: defaultdict[str, _NodeLoad],
    node_by_step: dict[float, str],
    cpu_usage: dict[float, float],
    cpu_request: dict[float, float],
    mem_usage: dict[float, float],
    mem_request: dict[float, float],
) -> None:
    """Add one pod's per-step usage/request to the node it ran on at that
    step, billing each step at max(usage, request) like `_billed_hours`.
    Steps with no known node (unscheduled, or unlabelled metrics) are
    skipped."""
    for ts, node in node_by_step.items():
        load = node_load[node]
        load.steps.add(ts)
        cpu_u = cpu_usage.get(ts, 0.0)
        cpu_r = cpu_request.get(ts, 0.0)
        mem_u = mem_usage.get(ts, 0.0)
        mem_r = mem_request.get(ts, 0.0)
        load.cpu_usage += cpu_u * STEP_HOURS
        load.cpu_request += cpu_r * STEP_HOURS
        load.cpu_billed += max(cpu_u, cpu_r) * STEP_HOURS
        load.mem_usage += mem_u * STEP_HOURS
        load.mem_request += mem_r * STEP_HOURS
        load.mem_billed += max(mem_u, mem_r) * STEP_HOURS


# ---------------------------------------------------------------------------
# Pod classification
# ---------------------------------------------------------------------------


class _ScopeCaches:
    def __init__(self) -> None:
        self.orgs: dict[str, Organization] = {}
        self.projects: dict[tuple[uuid.UUID | None, str], Project] = {}
        self.environments: dict[tuple[uuid.UUID, str], Environment] = {}
        self.applications: dict[tuple[uuid.UUID, str], Application] = {}
        self.app_envs_by_app: defaultdict[uuid.UUID, list[ApplicationEnvironment]] = (
            defaultdict(list)
        )
        self.resources_by_namespace: defaultdict[str, list[tuple[Resource, str]]] = (
            defaultdict(list)
        )
        #: Tenant namespace -> scope. Missing means platform.
        self.namespace_scope: dict[
            str, tuple[Organization, Project | None, Environment | None]
        ] = {}


def _build_scope_caches() -> _ScopeCaches:
    caches = _ScopeCaches()
    orgs: list[Organization] = Organization.query.all()
    for org in orgs:
        caches.orgs[org.k8s_identifier] = org
        caches.namespace_scope[org.k8s_identifier] = (org, None, None)
    orgs_by_id = {org.id: org for org in orgs}
    projects: list[Project] = Project.query.all()
    for project in projects:
        caches.projects[(project.organization_id, project.k8s_identifier)] = project
    environments: list[Environment] = Environment.query.all()
    for env in environments:
        caches.environments[(env.project_id, env.k8s_identifier)] = env
        if env.uses_environment_namespace:
            project = env.project
            org = (
                orgs_by_id.get(project.organization_id)
                if project is not None and project.organization_id is not None
                else None
            )
            if org is not None:
                caches.namespace_scope[env.k8s_namespace] = (org, project, env)
    apps: list[Application] = Application.query.all()
    for app in apps:
        caches.applications[(app.project_id, app.k8s_identifier)] = app
    app_envs: list[ApplicationEnvironment] = ApplicationEnvironment.query.filter(
        ApplicationEnvironment.deleted_at.is_(None)
    ).all()
    for app_env in app_envs:
        caches.app_envs_by_app[app_env.application_id].append(app_env)
    for app_env_list in caches.app_envs_by_app.values():
        app_env_list.sort(key=lambda ae: ae.created)
    resources: list[Resource] = Resource.query.options(
        joinedload(Resource.environment).joinedload(Environment.project)
    ).all()
    for resource in resources:
        namespace = resource.environment.k8s_namespace
        prefix = safe_k8s_name(
            resource.environment.project.k8s_identifier, resource.k8s_identifier
        )
        caches.resources_by_namespace[namespace].append((resource, prefix))
    return caches


def _classify_pod(
    namespace: str | None,
    pod_name: str,
    labels: dict[str, str],
    caches: _ScopeCaches,
    build_environment_by_job: dict[str, uuid.UUID],
) -> PodClassification | None:
    process = labels.get("label_process")
    if process == "build":
        return _classify_build_pod(pod_name, labels, caches, build_environment_by_job)

    app_k8s = labels.get("label_cabotage_io_application")
    if app_k8s:
        return _classify_app_pod(labels, caches, process)

    org_k8s = labels.get("label_cabotage_io_organization")
    project_k8s = labels.get("label_cabotage_io_project")
    if org_k8s and project_k8s:
        return _classify_backing_pod(namespace, pod_name, labels, caches)

    return None


def _default_app_env(
    application: Application, caches: _ScopeCaches
) -> ApplicationEnvironment | None:
    """The app's ApplicationEnvironment whose environment does not use an
    environment-scoped namespace, else the first/only app_env."""
    candidates = caches.app_envs_by_app.get(application.id) or []
    if not candidates:
        return None
    for app_env in candidates:
        if not app_env.environment.uses_environment_namespace:
            return app_env
    return candidates[0]


def _resolve_build_app_envs(job_ids: set[str]) -> dict[str, uuid.UUID]:
    """Map each build pod's job id to its environment_id, via the Image or
    Release row that ran the build."""
    if not job_ids:
        return {}

    app_env_id_by_job: dict[str, uuid.UUID] = {}
    images: list[Image] = Image.query.filter(Image.build_job_id.in_(job_ids)).all()
    for image in images:
        if (
            image.build_job_id
            and image.build_job_id not in app_env_id_by_job
            and image.application_environment_id
        ):
            app_env_id_by_job[image.build_job_id] = image.application_environment_id

    missing = job_ids - app_env_id_by_job.keys()
    if missing:
        releases: list[Release] = Release.query.filter(
            Release.build_job_id.in_(missing)
        ).all()
        for release in releases:
            if (
                release.build_job_id
                and release.build_job_id not in app_env_id_by_job
                and release.application_environment_id
            ):
                app_env_id_by_job[release.build_job_id] = (
                    release.application_environment_id
                )

    app_env_ids = set(app_env_id_by_job.values())
    if not app_env_ids:
        return {}
    found: list[ApplicationEnvironment] = ApplicationEnvironment.query.filter(
        ApplicationEnvironment.id.in_(app_env_ids)
    ).all()
    environment_id_by_app_env_id = {
        app_env.id: app_env.environment_id for app_env in found
    }
    return {
        job_id: environment_id_by_app_env_id[app_env_id]
        for job_id, app_env_id in app_env_id_by_job.items()
        if app_env_id in environment_id_by_app_env_id
    }


def _classify_build_pod(
    pod_name: str,
    labels: dict[str, str],
    caches: _ScopeCaches,
    build_environment_by_job: dict[str, uuid.UUID],
) -> PodClassification | None:
    org_k8s = labels.get("label_cabotage_io_organization")
    if not org_k8s:
        return None
    org = caches.orgs.get(org_k8s)
    if org is None:
        return None

    project_k8s = labels.get("label_cabotage_io_project")
    project = caches.projects.get((org.id, project_k8s)) if project_k8s else None
    app_k8s = labels.get("label_cabotage_io_application")
    application = (
        caches.applications.get((project.id, app_k8s))
        if project is not None and app_k8s
        else None
    )

    environment_id = None
    if application is not None:
        match = BUILD_POD_RE.match(pod_name)
        if match:
            environment_id = build_environment_by_job.get(match.group(1))
        if environment_id is None:
            app_env = _default_app_env(application, caches)
            if app_env is not None:
                environment_id = app_env.environment_id

    return {
        "category": "build",
        "organization_id": org.id,
        "project_id": project.id if project is not None else None,
        "environment_id": environment_id,
        "application_id": application.id if application is not None else None,
        "resource_id": None,
        "component": "build",
    }


def _classify_app_pod(
    labels: dict[str, str],
    caches: _ScopeCaches,
    process: str | None,
) -> PodClassification | None:
    org_k8s = labels.get("label_cabotage_io_organization")
    org = caches.orgs.get(org_k8s) if org_k8s else None
    if org is None:
        return None

    project_k8s = labels.get("label_cabotage_io_project")
    project = caches.projects.get((org.id, project_k8s)) if project_k8s else None
    app_k8s = labels.get("label_cabotage_io_application")
    application = (
        caches.applications.get((project.id, app_k8s))
        if project is not None and app_k8s
        else None
    )

    environment_id = None
    if application is not None:
        env_k8s = labels.get("label_cabotage_io_environment")
        if env_k8s and project is not None:
            env = caches.environments.get((project.id, env_k8s))
            environment_id = env.id if env is not None else None
        else:
            app_env = _default_app_env(application, caches)
            if app_env is not None:
                environment_id = app_env.environment_id

    return {
        "category": "compute",
        "organization_id": org.id,
        "project_id": project.id if project is not None else None,
        "environment_id": environment_id,
        "application_id": application.id if application is not None else None,
        "resource_id": None,
        "component": process or "unknown",
    }


def _classify_backing_pod(
    namespace: str | None,
    pod_name: str,
    labels: dict[str, str],
    caches: _ScopeCaches,
) -> PodClassification | None:
    org_k8s = labels.get("label_cabotage_io_organization")
    org = caches.orgs.get(org_k8s) if org_k8s else None
    if org is None:
        return None

    project_k8s = labels.get("label_cabotage_io_project")
    project = caches.projects.get((org.id, project_k8s)) if project_k8s else None

    environment_id = None
    env_k8s = labels.get("label_cabotage_io_environment")
    if project is not None and env_k8s:
        env = caches.environments.get((project.id, env_k8s))
        environment_id = env.id if env is not None else None

    matched_resource: Resource | None = None
    for resource, prefix in caches.resources_by_namespace.get(namespace or "", []):
        if pod_name.startswith(prefix + "-"):
            matched_resource = resource
            break

    component = (
        matched_resource.slug[:COMPONENT_MAX_LEN]
        if matched_resource is not None
        else "other"
    )

    return {
        "category": "backing_service",
        "organization_id": org.id,
        "project_id": project.id if project is not None else None,
        "environment_id": environment_id,
        "application_id": None,
        "resource_id": matched_resource.id if matched_resource is not None else None,
        "component": component,
    }


# ---------------------------------------------------------------------------
# Platform workloads
# ---------------------------------------------------------------------------


_STATEFULSET_ORDINAL_RE = re.compile(r"^(.+)-(\d+)$")
_REPLICASET_POD_RE = re.compile(r"^(.+)-[a-z0-9]{8,10}-[a-z0-9]{5}$")
_GENERATED_SUFFIX_RE = re.compile(r"^(.+)-[a-z0-9]{5}$")


def _claims_tenant(
    namespace: str | None, labels: dict[str, str] | None, caches: _ScopeCaches
) -> bool:
    """Whether an unclassifiable pod is a tenant's (unallocated) rather than
    platform. Without labels, only the namespace can tell."""
    if labels is None:
        return namespace in caches.namespace_scope
    return bool(
        labels.get("label_cabotage_io_organization") or labels.get("label_organization")
    )


def _platform_pod_workload(pod_name: str) -> str:
    """``mimir-write-0`` -> ``mimir-write``, ``traefik-5d8f7c9b4-x2k9p`` ->
    ``traefik``, so replicas roll up into one component."""
    for pattern in (
        _STATEFULSET_ORDINAL_RE,
        _REPLICASET_POD_RE,
        _GENERATED_SUFFIX_RE,
    ):
        match = pattern.match(pod_name)
        if match:
            return match.group(1)
    return pod_name


def _platform_claim_workload(claim_name: str) -> str:
    """``data-mimir-write-0`` -> ``data-mimir-write``."""
    match = _STATEFULSET_ORDINAL_RE.match(claim_name)
    if match:
        return match.group(1)
    return claim_name


def _empty_platform_stats() -> PlatformComponentStats:
    return {
        "cpu_billed_hours": 0.0,
        "memory_billed_gib_hours": 0.0,
        "network_gb": 0.0,
        "storage_gib_month": 0.0,
        "cost_usd": 0.0,
    }


def _add_platform(
    platform_components: defaultdict[str, PlatformComponentStats],
    name: str,
    *,
    cpu_billed_hours: float = 0.0,
    memory_billed_gib_hours: float = 0.0,
    network_gb: float = 0.0,
    storage_gib_month: float = 0.0,
    cost_usd: float = 0.0,
) -> None:
    entry = platform_components[name]
    entry["cpu_billed_hours"] += cpu_billed_hours
    entry["memory_billed_gib_hours"] += memory_billed_gib_hours
    entry["network_gb"] += network_gb
    entry["storage_gib_month"] += storage_gib_month
    entry["cost_usd"] += cost_usd


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _collect_storage(
    period_start: datetime.datetime, period_end: datetime.datetime, rates: CostRates
) -> list[CostLineItem]:
    items: list[CostLineItem] = []

    # Resource.created/deleted_at are stored as naive UTC datetimes (no
    # tzinfo), while period_start/period_end are timezone-aware UTC; strip
    # tzinfo for the comparison rather than making the DB columns aware.
    naive_period_start = period_start.replace(tzinfo=None)
    naive_period_end = period_end.replace(tzinfo=None)

    def _existed_during_hour(resource: Resource) -> bool:
        if resource.created is not None and resource.created > naive_period_end:
            return False
        return not (
            resource.deleted_at is not None
            and resource.deleted_at <= naive_period_start
        )

    postgres_resources: list[PostgresResource] = PostgresResource.query.all()
    for pg_resource in postgres_resources:
        if not _existed_during_hour(pg_resource):
            continue
        instances = 2 if pg_resource.ha_enabled else 1
        quantity = (pg_resource.storage_size * instances) / HOURS_PER_MONTH
        cost = (
            volume_monthly_cost(pg_resource.storage_size, rates)
            * instances
            / HOURS_PER_MONTH
        )
        env = pg_resource.environment
        items.append(
            CostLineItem(
                organization_id=env.project.organization_id,
                project_id=env.project_id,
                environment_id=env.id,
                application_id=None,
                resource_id=pg_resource.id,
                category="storage",
                meter="storage",
                component=pg_resource.slug[:COMPONENT_MAX_LEN],
                quantity=quantity,
                unit="GiB-mo",
                usage_quantity=None,
                request_quantity=None,
                cost_usd=cost,
            )
        )

    redis_resources: list[RedisResource] = RedisResource.query.all()
    for redis_resource in redis_resources:
        if not _existed_during_hour(redis_resource):
            continue
        # Standalone Redis is one instance; the replica counts only apply to HA.
        instances = (
            redis_resource.leader_replicas + redis_resource.follower_replicas
            if redis_resource.ha_enabled
            else 1
        )
        quantity = (redis_resource.storage_size * instances) / HOURS_PER_MONTH
        cost = (
            volume_monthly_cost(redis_resource.storage_size, rates)
            * instances
            / HOURS_PER_MONTH
        )
        env = redis_resource.environment
        items.append(
            CostLineItem(
                organization_id=env.project.organization_id,
                project_id=env.project_id,
                environment_id=env.id,
                application_id=None,
                resource_id=redis_resource.id,
                category="storage",
                meter="storage",
                component=redis_resource.slug[:COMPONENT_MAX_LEN],
                quantity=quantity,
                unit="GiB-mo",
                usage_quantity=None,
                request_quantity=None,
                cost_usd=cost,
            )
        )

    return items


def _collect_pv_storage(
    period_end: datetime.datetime,
    rates: CostRates,
    caches: _ScopeCaches,
    platform_components: defaultdict[str, PlatformComponentStats],
) -> tuple[list[CostLineItem], str | None]:
    """Price every bound PersistentVolume, skipping Cabotage Postgres/Redis
    (priced in `_collect_storage`). Claims with tenant ``cabotage.io/*``
    labels become storage line items; the rest are platform components."""
    items: list[CostLineItem] = []
    if not current_app.config.get("KUBERNETES_ENABLED"):
        return items, None
    try:
        api_client = kubernetes_ext.kubernetes_client
        core_api = kubernetes.client.CoreV1Api(api_client)
        volumes = core_api.list_persistent_volume()
        claims = core_api.list_persistent_volume_claim_for_all_namespaces()
    except ApiException as exc:
        return items, f"listing persistent volumes failed: {exc.status} {exc.reason}"
    except Exception as exc:
        return items, f"listing persistent volumes failed: {type(exc).__name__}: {exc}"
    claim_labels: dict[tuple[str, str], dict[str, str]] = {
        (claim.metadata.namespace, claim.metadata.name): claim.metadata.labels or {}
        for claim in claims.items
        if claim.metadata is not None
        and claim.metadata.namespace
        and claim.metadata.name
    }

    for volume in volumes.items:
        spec = volume.spec
        if spec is None:
            continue
        created = volume.metadata.creation_timestamp if volume.metadata else None
        if created is not None and created >= period_end:
            continue
        capacity = spec.capacity or {}
        gib = parse_memory_quantity_gib(capacity.get("storage"))
        if gib is None:
            continue
        phase = volume.status.phase if volume.status is not None else None
        if phase != "Bound":
            continue
        gib_month = gib / HOURS_PER_MONTH
        cost = volume_monthly_cost(gib, rates) / HOURS_PER_MONTH

        claim_ref = spec.claim_ref
        claim_namespace = claim_ref.namespace if claim_ref is not None else None
        claim_name = claim_ref.name if claim_ref is not None else None
        if not claim_namespace or not claim_name:
            _add_platform(
                platform_components,
                "unclaimed/volumes",
                storage_gib_month=gib_month,
                cost_usd=cost,
            )
            continue

        # Already priced from the resource's storage_size.
        if any(
            claim_name.startswith(prefix + "-")
            for _, prefix in caches.resources_by_namespace.get(claim_namespace, [])
        ):
            continue

        scope = _claim_scope(
            claim_labels.get((claim_namespace, claim_name), {}), caches
        )
        if scope is None:
            name = f"{claim_namespace}/{_platform_claim_workload(claim_name)}"
            _add_platform(
                platform_components,
                name,
                storage_gib_month=gib_month,
                cost_usd=cost,
            )
            continue

        org, project, environment = scope
        items.append(
            CostLineItem(
                organization_id=org.id,
                project_id=project.id if project is not None else None,
                environment_id=environment.id if environment is not None else None,
                application_id=None,
                resource_id=None,
                category="storage",
                meter="storage",
                component=claim_name[:COMPONENT_MAX_LEN],
                quantity=gib_month,
                unit="GiB-mo",
                usage_quantity=None,
                request_quantity=None,
                cost_usd=cost,
            )
        )

    return items, None


def _claim_scope(
    labels: dict[str, str], caches: _ScopeCaches
) -> tuple[Organization, Project | None, Environment | None] | None:
    """The tenant a PVC's ``cabotage.io/*`` labels resolve to, if any."""
    org_k8s = labels.get("cabotage.io/organization")
    org = caches.orgs.get(org_k8s) if org_k8s else None
    if org is None:
        return None
    project_k8s = labels.get("cabotage.io/project")
    project = caches.projects.get((org.id, project_k8s)) if project_k8s else None
    env_k8s = labels.get("cabotage.io/environment")
    environment = (
        caches.environments.get((project.id, env_k8s))
        if project is not None and env_k8s
        else None
    )
    return org, project, environment


# ---------------------------------------------------------------------------
# Egress
# ---------------------------------------------------------------------------


def _observe_namespace(app_env: ApplicationEnvironment) -> str:
    org_k8s = app_env.application.project.organization.k8s_identifier
    if app_env.environment.uses_environment_namespace:
        return app_env.environment.k8s_namespace
    return org_k8s


def _observe_prefix(application: Application) -> str:
    return safe_k8s_name(application.project.k8s_identifier, application.k8s_identifier)


def _build_traefik_service_map() -> dict[str, tuple[ApplicationEnvironment, str]]:
    """Map traefik service name -> (ApplicationEnvironment, process name)."""
    service_map: dict[str, tuple[ApplicationEnvironment, str]] = {}
    app_envs: list[ApplicationEnvironment] = (
        ApplicationEnvironment.query.filter(ApplicationEnvironment.deleted_at.is_(None))
        .options(
            joinedload(ApplicationEnvironment.environment),
            joinedload(ApplicationEnvironment.application)
            .joinedload(Application.project)
            .joinedload(Project.organization),
            selectinload(ApplicationEnvironment.ingresses).selectinload(Ingress.paths),
        )
        .all()
    )
    for app_env in app_envs:
        application = app_env.application
        if application is None or application.deleted_at is not None:
            continue
        # projects.organization_id is nullable; with no org there is no
        # namespace, so the app can't own any traefik service.
        if application.project.organization is None:
            continue
        namespace = _observe_namespace(app_env)
        prefix = _observe_prefix(application)
        for ingress in app_env.ingresses:
            if not ingress.enabled:
                continue
            processes = {path.target_process_name for path in ingress.paths}
            if not processes:
                processes = {"web"}
            for process in processes:
                for suffix in ("https", "8000"):
                    name = (
                        f"{namespace}-{prefix}-{ingress.name}-{prefix}-"
                        f"{process}-{suffix}@kubernetesingressnginx"
                    )
                    service_map[name] = (app_env, process)
    return service_map


def _collect_egress(
    traefik_result: list[MimirSeries], rates: CostRates
) -> list[CostLineItem]:
    items: list[CostLineItem] = []
    service_map = _build_traefik_service_map()
    rate = rates["egress_gb"]

    totals: defaultdict[tuple[ApplicationEnvironment, str], float] = defaultdict(float)
    for series in traefik_result or []:
        service = series.get("metric", {}).get("service")
        if not service:
            continue
        mapped = service_map.get(service)
        if mapped is None:
            continue
        series_value = series.get("value")
        try:
            value = float(series_value[1]) if series_value else 0.0
        except TypeError, ValueError, IndexError:
            value = 0.0
        totals[mapped] += value

    for (app_env, process), total_bytes in totals.items():
        gb = total_bytes / 1e9
        cost = gb * rate
        application = app_env.application
        items.append(
            CostLineItem(
                organization_id=application.project.organization_id,
                project_id=application.project_id,
                environment_id=app_env.environment_id,
                application_id=application.id,
                resource_id=None,
                category="egress",
                meter="egress",
                component=process,
                quantity=gb,
                unit="GB",
                usage_quantity=None,
                request_quantity=None,
                cost_usd=cost,
            )
        )
    return items


# ---------------------------------------------------------------------------
# Overhead / nodes
# ---------------------------------------------------------------------------


def _collect_nodes(
    node_load: dict[str, _NodeLoad], rates: CostRates
) -> tuple[list[CostNodeHour], str | None]:
    """Price every node that ran pods this hour and split its cost into
    requested / used / idle. Returns the rows plus why the node inventory
    couldn't be refreshed, if it couldn't (nodes it has never seen stay
    unpriced)."""
    inventory_error = _refresh_nodes()
    nodes: dict[str, CostNode] = {
        node.name: node
        for node in CostNode.query.filter(CostNode.name.in_(list(node_load))).all()
    }
    rows: list[CostNodeHour] = []
    for node_name, load in sorted(node_load.items()):
        node = nodes.get(node_name)
        row = CostNodeHour(
            node_name=node_name,
            instance_type=node.instance_type if node is not None else None,
            capacity_type=node.capacity_type if node is not None else None,
            cpu_capacity=node.cpu_capacity if node is not None else None,
            memory_gib=node.memory_gib if node is not None else None,
            node_hours=min(len(load.steps), STEPS_PER_HOUR) / STEPS_PER_HOUR,
            cpu_request_hours=load.cpu_request,
            cpu_usage_hours=load.cpu_usage,
            cpu_billed_hours=load.cpu_billed,
            memory_request_gib_hours=load.mem_request,
            memory_usage_gib_hours=load.mem_usage,
            memory_billed_gib_hours=load.mem_billed,
        )
        if node is not None and node.instance_type is not None:
            _price_node_hour(row, node.instance_type, load, rates)
        rows.append(row)
    return rows, inventory_error


def _price_node_hour(
    row: CostNodeHour, instance_type: str, load: _NodeLoad, rates: CostRates
) -> None:
    """Set the row's dollar columns. The node's price is split across its
    vCPU and memory in proportion to the list rates, so a requested vCPU on
    this node costs its share of what the node actually costs; idle is
    whatever no pod was billed for. Without known capacity only the total
    can be priced."""
    hourly = node_hourly_price(
        instance_type,
        rates,
        cpu_capacity=row.cpu_capacity,
        memory_gib=row.memory_gib,
    )
    cost = row.node_hours * hourly
    row.cost_usd = cost
    capacity_value = (row.cpu_capacity or 0.0) * rates["vcpu_hour"] + (
        row.memory_gib or 0.0
    ) * rates["memory_gib_hour"]
    if capacity_value <= 0:
        return
    scale = hourly / capacity_value

    def _value(cpu_hours: float, mem_gib_hours: float) -> float:
        value = scale * (
            cpu_hours * rates["vcpu_hour"] + mem_gib_hours * rates["memory_gib_hour"]
        )
        return min(max(value, 0.0), cost)

    row.requested_cost_usd = _value(load.cpu_request, load.mem_request)
    row.used_cost_usd = _value(load.cpu_usage, load.mem_usage)
    row.idle_cost_usd = cost - _value(load.cpu_billed, load.mem_billed)


def _refresh_nodes() -> str | None:
    """Upsert `CostNode` from the Kubernetes node list; returns why it
    couldn't, or None on success or when Kubernetes is disabled."""
    if not current_app.config.get("KUBERNETES_ENABLED"):
        return None
    try:
        api_client = kubernetes_ext.kubernetes_client
        core_api = kubernetes.client.CoreV1Api(api_client)
        nodes = core_api.list_node()
    except ApiException as exc:
        return f"listing nodes failed: {exc.status} {exc.reason}"
    except Exception as exc:
        return f"listing nodes failed: {type(exc).__name__}: {exc}"

    now = datetime.datetime.now(datetime.UTC)
    existing_by_name: dict[str, CostNode] = {
        node.name: node for node in CostNode.query.all()
    }
    for node in nodes.items:
        if node.metadata is None or node.metadata.name is None:
            continue
        name = node.metadata.name
        labels = node.metadata.labels or {}
        instance_type = labels.get("node.kubernetes.io/instance-type")
        capacity_type = labels.get("karpenter.sh/capacity-type")
        capacity = (node.status.capacity or {}) if node.status is not None else {}
        cpu_capacity = parse_cpu_quantity(capacity.get("cpu"))
        memory_gib = parse_memory_quantity_gib(capacity.get("memory"))

        record = existing_by_name.get(name)
        if record is None:
            record = CostNode(name=name)
            existing_by_name[name] = record
        record.instance_type = instance_type
        record.capacity_type = capacity_type
        record.cpu_capacity = cpu_capacity
        record.memory_gib = memory_gib
        record.last_seen = now
        db.session.add(record)
    db.session.flush()
    return None
