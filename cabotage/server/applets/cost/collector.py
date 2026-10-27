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
import struct
import uuid
from collections import defaultdict

import kubernetes.client
import sqlalchemy as sa
from flask import current_app
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
    NODE_PRESENCE_QUERY,
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
    PodClassification,
    PodKey,
    ScopeKey,
)
from cabotage.server.models.auth import Organization
from cabotage.server.models.cost import (
    CostCollectionRun,
    CostLineItem,
    CostNode,
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
        line_items, stats = _collect(period_start, period_end, rates)
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
        )

    return _write_run(
        period_start,
        started_at,
        rates,
        status="succeeded",
        error=None,
        stats=stats,
        line_items=line_items,
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

    CostLineItem.query.filter_by(period_start=period_start).delete()
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
    db.session.commit()
    return run


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _collect(
    period_start: datetime.datetime, period_end: datetime.datetime, rates: CostRates
) -> tuple[list[CostLineItem], CostRunStats]:
    # TODO: Include anonymous until migration to multi-tenant is completed
    # (mirrors _mimir_connection in cabotage.server.user.views).
    base_tenant = current_app.config.get("MIMIR_TENANT_ID")
    tenant_id = f"{base_tenant}|anonymous" if base_tenant else "anonymous"
    range_start = int(period_start.timestamp())
    range_end = int((period_start + datetime.timedelta(minutes=55)).timestamp())
    instant_time = int(period_end.timestamp())

    cpu_usage = _series_by_pod(
        mimir.query_range(
            CPU_USAGE_QUERY, range_start, range_end, STEP_SECONDS, tenant_id=tenant_id
        )
    )
    mem_usage = _series_by_pod_scaled(
        mimir.query_range(
            MEM_USAGE_QUERY, range_start, range_end, STEP_SECONDS, tenant_id=tenant_id
        ),
        1.0 / GIB,
    )
    cpu_request = _series_by_pod(
        mimir.query_range(
            CPU_REQUEST_QUERY,
            range_start,
            range_end,
            STEP_SECONDS,
            tenant_id=tenant_id,
        )
    )
    mem_request = _series_by_pod_scaled(
        mimir.query_range(
            MEM_REQUEST_QUERY,
            range_start,
            range_end,
            STEP_SECONDS,
            tenant_id=tenant_id,
        ),
        1.0 / GIB,
    )
    node_presence_result = mimir.query_range(
        NODE_PRESENCE_QUERY, range_start, range_end, STEP_SECONDS, tenant_id=tenant_id
    )
    pod_labels_result = mimir.query_instant(
        POD_LABELS_QUERY, tenant_id=tenant_id, time=instant_time
    )
    traefik_result = mimir.query_instant(
        TRAEFIK_QUERY, tenant_id=tenant_id, time=instant_time
    )

    pod_labels: dict[PodKey, dict[str, str]] = {}
    for series in pod_labels_result:
        metric = series.get("metric", {})
        pod_labels[(metric.get("namespace"), metric.get("pod"))] = metric

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
        }
    )

    all_pod_keys: set[PodKey] = (
        set(cpu_usage) | set(mem_usage) | set(cpu_request) | set(mem_request)
    )
    if not all_pod_keys:
        raise RuntimeError("no pod metrics for hour")
    # Labels come from kube-state-metrics, usage from cAdvisor; a KSM gap
    # would otherwise record a "succeeded" hour with every pod unallocated.
    if not pod_labels:
        raise RuntimeError("no pod labels for hour")
    for pod_key in all_pod_keys:
        namespace, pod_name = pod_key
        if not namespace or not pod_name:
            continue
        pods_seen += 1
        labels = pod_labels.get(pod_key)
        if labels is None:
            pods_unallocated += 1
            continue
        classification = _classify_pod(
            namespace, pod_name, labels, caches, build_environment_by_job
        )
        if classification is None:
            pods_unallocated += 1
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

    # --- Storage -----------------------------------------------------
    storage_items = _collect_storage(period_start, period_end, rates)
    line_items.extend(storage_items)

    # --- Egress --------------------------------------------------------
    egress_items = _collect_egress(traefik_result, rates)
    line_items.extend(egress_items)

    # --- Overhead --------------------------------------------------------
    node_hours, cluster_compute_cost, nodes_unpriced, node_data_available = (
        _collect_overhead_nodes(node_presence_result, rates)
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

    stats: CostRunStats = {
        "pods_seen": pods_seen,
        "pods_unallocated": pods_unallocated,
        "node_hours": node_hours,
        "nodes_unpriced": nodes_unpriced,
        "node_data_available": node_data_available,
        "cluster_compute_cost": cluster_compute_cost,
        "tenant_compute_cost": direct_compute_total,
        "overhead_pool": overhead_pool,
        "overhead_allocated": overhead_allocated,
        "fixed_overhead": fixed_overhead,
    }

    return line_items, stats


# ---------------------------------------------------------------------------
# Mimir series helpers
# ---------------------------------------------------------------------------


def _series_by_pod(
    result: list[MimirSeries] | None,
) -> dict[PodKey, dict[float, float]]:
    """Map a Mimir range-query result (summed by namespace,pod) to
    ``{(namespace, pod): {timestamp: value}}``."""
    out: dict[PodKey, dict[float, float]] = {}
    for series in result or []:
        metric = series.get("metric", {})
        key = (metric.get("namespace"), metric.get("pod"))
        points: dict[float, float] = {}
        for ts, val in series.get("values", []):
            try:
                points[float(ts)] = float(val)
            except TypeError, ValueError:
                continue
        out[key] = points
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


def _build_scope_caches() -> _ScopeCaches:
    caches = _ScopeCaches()
    orgs: list[Organization] = Organization.query.all()
    for org in orgs:
        caches.orgs[org.k8s_identifier] = org
    projects: list[Project] = Project.query.all()
    for project in projects:
        caches.projects[(project.organization_id, project.k8s_identifier)] = project
    environments: list[Environment] = Environment.query.all()
    for env in environments:
        caches.environments[(env.project_id, env.k8s_identifier)] = env
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
        if app_env.application is None or app_env.application.deleted_at is not None:
            continue
        namespace = _observe_namespace(app_env)
        prefix = _observe_prefix(app_env.application)
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


def _collect_overhead_nodes(
    node_presence_result: list[MimirSeries], rates: CostRates
) -> tuple[float, float, int, bool]:
    _refresh_nodes()

    present_steps: defaultdict[str, int] = defaultdict(int)
    for series in node_presence_result or []:
        instance = series.get("metric", {}).get("instance")
        if not instance:
            continue
        node_name = instance.rsplit(":", 1)[0] if ":" in instance else instance
        timestamps = {ts for ts, _ in series.get("values", [])}
        present_steps[node_name] += len(timestamps)

    if not present_steps:
        return 0.0, 0.0, 0, False

    cost_nodes: list[CostNode] = CostNode.query.all()
    nodes = {node.name: node for node in cost_nodes}
    node_hours = 0.0
    cluster_compute_cost = 0.0
    nodes_unpriced = 0
    for node_name, steps in present_steps.items():
        fraction = min(steps, STEPS_PER_HOUR) / STEPS_PER_HOUR
        node_hours += fraction
        node = nodes.get(node_name)
        if node is None or node.instance_type is None:
            nodes_unpriced += 1
            continue
        price = node_hourly_price(
            node.instance_type,
            rates,
            cpu_capacity=node.cpu_capacity,
            memory_gib=node.memory_gib,
        )
        cluster_compute_cost += fraction * price

    return node_hours, cluster_compute_cost, nodes_unpriced, True


def _refresh_nodes() -> None:
    if not current_app.config.get("KUBERNETES_ENABLED"):
        return
    try:
        api_client = kubernetes_ext.kubernetes_client
        core_api = kubernetes.client.CoreV1Api(api_client)
        nodes = core_api.list_node()
    except Exception:
        return

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
