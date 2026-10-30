"""Synthetic cost history for local development.

``seed_cost_history(organization, days=90, now=None)`` writes realistic
``CostCollectionRun``/``CostLineItem`` rows for every enrolled app_env and
resource of an organization, derived from each process's pod class
requests and replica counts, with diurnal usage curves, occasional build
activity, egress proportional to web traffic, resource storage, and a
shared-overhead split — plus one deliberately failed run ~3 days ago so the
freshness UI has something to show.

Idempotent and scale-safe:

- Rows are written via raw ``COPY`` on the session's underlying psycopg
  connection (not one ORM ``session.add()`` per row), so seeding a
  PyPI-scale organization (dozens of apps, hundreds of processes) over 90
  days of hourly history stays fast — low hundreds of thousands of
  ``CostLineItem`` rows per organization in a few seconds, not minutes.
- Every demo-written ``CostCollectionRun`` is tagged ``stats["seed"] ==
  "demo"``. Re-seeding only deletes+recreates hours carrying that tag; any
  hour whose existing run lacks the tag is treated as real collector
  output from the collection task and is left alone
  and skipped entirely, so a later demo reseed never clobbers real data.
"""

import datetime
import math
import random
import uuid
from typing import cast

import psycopg
from psycopg import sql as pg_sql
from psycopg.types.json import Json
from sqlalchemy import func, select

from cabotage.server import db
from cabotage.server.applets.cost.constants import (
    DEMO_BUILD_CPU_REQUEST,
    DEMO_BUILD_HOUR_PROBABILITY,
    DEMO_BUILD_HOURS,
    DEMO_BUILD_MEM_REQUEST,
    HOURS_PER_MONTH,
    LINE_ITEM_COLUMNS,
    NODE_HOUR_COLUMNS,
    RUN_COLUMNS,
)
from cabotage.server.applets.cost.helpers import (
    as_utc,
    parse_cpu_quantity,
    parse_memory_quantity_gib,
)
from cabotage.server.applets.cost.rates import effective_rates, node_hourly_price
from cabotage.server.applets.cost.types import (
    CostRates,
    Events,
    ProcessEntry,
    ResourceEntry,
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
    DEFAULT_POD_CLASS,
    Application,
    pod_classes,
)
from cabotage.server.models.resources import (
    RedisResource,
    Resource,
    postgres_size_classes,
    redis_size_classes,
)


def _copy_rows(
    cursor: psycopg.Cursor,
    table: str,
    columns: tuple[str, ...],
    rows: list[tuple[object, ...]],
) -> None:
    """Bulk-insert ``rows`` (tuples matching ``columns``) via COPY FROM STDIN."""
    if not rows:
        return
    copy_sql = pg_sql.SQL("COPY {} ({}) FROM STDIN").format(
        pg_sql.Identifier(table),
        pg_sql.SQL(", ").join(pg_sql.Identifier(c) for c in columns),
    )
    with cursor.copy(copy_sql) as copy:
        for row in rows:
            copy.write_row(row)


def seed_cost_history(
    organization: Organization, days: int = 90, now: datetime.datetime | None = None
) -> int:
    """Seed `days` days of synthetic hourly cost history for `organization`.

    Returns the number of CostLineItem rows written.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.UTC)

    last_hour = now.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(
        hours=1
    )
    first_hour = last_hour - datetime.timedelta(hours=days * 24 - 1)

    # Clear this organization's own demo CostLineItem rows that fall outside the
    # window we're about to (re)write, e.g. leftovers from a previous call
    # with a larger `days`. Scoped to CostLineItem only (not
    # CostCollectionRun, which is cluster-wide and shared across orgs).
    demo_run_ids = select(CostCollectionRun.id).where(
        func.jsonb_extract_path_text(CostCollectionRun.stats, "seed") == "demo"
    )
    CostLineItem.query.filter(
        CostLineItem.organization_id == organization.id,
        CostLineItem.period_start < first_hour,
        CostLineItem.run_id.in_(demo_run_ids),
    ).delete(synchronize_session=False)

    # CostCollectionRun is ONE row per cluster-wide hour, shared by every
    # organization's CostLineItem rows for that hour (exactly like the real
    # collector: one collect_hour() pass covers the whole cluster). Seeding
    # multiple organizations must therefore REUSE a shared hour's run
    # rather than deleting/recreating it, or one org's reseed would cascade
    # -delete every other org's line items for that hour.
    #
    # For each hour in the window, classify the existing run (if any):
    #   - no stats["seed"] == "demo"  -> real collector output; never touch,
    #     skip this hour for every org.
    #   - demo-tagged and status="failed" -> a shared "no data this hour"
    #     beat (demo or real collectors both fail cluster-wide, never per
    #     org); skip this hour for every org too.
    #   - demo-tagged and status="succeeded" -> reuse its id; only this
    #     organization's own line items under that run get replaced.
    existing_runs: list[CostCollectionRun] = CostCollectionRun.query.filter(
        CostCollectionRun.period_start >= first_hour,
        CostCollectionRun.period_start <= last_hour,
    ).all()
    skip_hours: set[datetime.datetime] = set()
    reusable_run_id_by_hour: dict[datetime.datetime, uuid.UUID] = {}
    for run in existing_runs:
        is_demo = (run.stats or {}).get("seed") == "demo"
        if not is_demo or run.status == "failed":
            skip_hours.add(run.period_start)
        else:
            reusable_run_id_by_hour[run.period_start] = run.id

    if reusable_run_id_by_hour:
        # Replace only this organization's own rows under reused runs;
        # other organizations' rows on the same run_id are untouched.
        CostLineItem.query.filter(
            CostLineItem.organization_id == organization.id,
            CostLineItem.run_id.in_(reusable_run_id_by_hour.values()),
        ).delete(synchronize_session=False)
    db.session.flush()

    rates = effective_rates()
    rng = random.Random(f"cost-demo-{organization.id}")  # nosec B311

    processes, applications = _collect_processes(organization)
    resources = _collect_resources(organization)

    total_hours = days * 24
    failed_offset = min(total_hours - 1, rng.randint(70, 76))  # ~3 days ago
    failed_hour = last_hour - datetime.timedelta(hours=failed_offset)

    # A few narrative events so the explorer has stories to show, beyond
    # flat random noise:
    #  - weekly seasonality (handled per-hour via _weekly_factor)
    #  - a one-day traffic spike ~45 days ago
    #  - a fleet-wide scale-up ~30 days ago (higher baseline requests after)
    #  - one process that only "appears" ~20 days ago (a newly enrolled app)
    #  - one application soft-deleted a couple of days ago, so its history
    #    still shows up with deleted=True in breakdowns
    spike_date = (last_hour - datetime.timedelta(days=45)).date()
    scale_up_cutoff = last_hour - datetime.timedelta(days=30)
    new_proc_cutoff = last_hour - datetime.timedelta(days=20)

    if len(processes) > 1:
        new_proc = rng.choice(processes)
        new_proc["appears_after"] = new_proc_cutoff

    # Never soft-delete an org's biggest app (by total replica count) — this
    # runs per-org now (seed_cost_history is called once per organization),
    # and a naive `rng.choice(applications)` had a real chance of nuking an
    # org's flagship app (e.g. a 2-app org's main app) purely by bad luck.
    # Require at least 3 applications and exclude the single largest.
    # Skip once the org has a soft-deleted app, so reseeds don't delete another.
    already_decommissioned = any(
        a.deleted_at is not None
        for p in organization.active_projects
        for a in p.project_applications
    )
    if len(applications) > 2 and not already_decommissioned:

        def _app_weight(a: Application) -> int:
            return sum(
                sum((ae.process_counts or {}).values())
                for ae in a.active_application_environments
            )

        candidates = sorted(applications, key=_app_weight)[:-1]
        if candidates:
            decommissioned = rng.choice(candidates)
            decommissioned.deleted_at = now - datetime.timedelta(days=2)
            db.session.add(decommissioned)
            for proc in processes:
                if proc["application_id"] == decommissioned.id:
                    proc["gone_after"] = decommissioned.deleted_at

    events: Events = {
        "spike_date": spike_date,
        "scale_up_cutoff": scale_up_cutoff,
    }

    # Build every row in memory (as plain tuples, not ORM-tracked objects)
    # and write them with one COPY per table at the end: at PyPI scale this
    # is the difference between seconds and minutes for 90 days x hundreds
    # of processes/resources per organization.
    run_rows: list[tuple[object, ...]] = []
    item_rows: list[tuple[object, ...]] = []
    total_items = 0
    hour = first_hour
    while hour <= last_hour:
        if hour in skip_hours:
            hour += datetime.timedelta(hours=1)
            continue

        # The real beat task collects an hour at minute=7 of the hour right
        # after it ends, e.g. the 15:00-16:00 hour is collected at ~16:07.
        collected_at = hour + datetime.timedelta(hours=1, minutes=7)
        reused_run_id = reusable_run_id_by_hour.get(hour)

        if reused_run_id is None and hour == failed_hour:
            # Only the organization that first reaches this hour gets to
            # mark it as the shared failed hour; later organizations will
            # see it in `reusable_run_id_by_hour` (if succeeded) or
            # `skip_hours` (if another org's failed_hour landed here
            # first) on their own call.
            run_rows.append(
                (
                    uuid.uuid4(),
                    hour,
                    "failed",
                    collected_at,
                    collected_at + datetime.timedelta(seconds=20),
                    "Mimir query_range request failed: connection reset by peer",
                    Json(rates),
                    Json({"seed": "demo"}),
                )
            )
            hour += datetime.timedelta(hours=1)
            continue

        run_id = reused_run_id if reused_run_id is not None else uuid.uuid4()
        items, stats = _generate_hour(hour, processes, resources, rates, rng, events)
        if reused_run_id is None:
            run_rows.append(
                (
                    run_id,
                    hour,
                    "succeeded",
                    collected_at,
                    collected_at + datetime.timedelta(seconds=40),
                    None,
                    Json(rates),
                    Json({**stats, "seed": "demo"}),
                )
            )
        for item in items:
            item_rows.append(
                (
                    uuid.uuid4(),
                    run_id,
                    hour,
                    item.organization_id,
                    item.project_id,
                    item.environment_id,
                    item.application_id,
                    item.resource_id,
                    item.category,
                    item.meter,
                    item.component,
                    item.quantity,
                    item.unit,
                    item.usage_quantity,
                    item.request_quantity,
                    item.cost_usd,
                )
            )
        total_items += len(items)
        hour += datetime.timedelta(hours=1)

    raw_connection = db.session.connection().connection.dbapi_connection
    if raw_connection is None:
        raise RuntimeError("Demo cost seeding requires a database connection")
    cursor = cast(psycopg.Cursor, raw_connection.cursor())
    _copy_rows(cursor, "cost_collection_runs", RUN_COLUMNS, run_rows)
    _copy_rows(cursor, "cost_line_items", LINE_ITEM_COLUMNS, item_rows)
    db.session.commit()
    return total_items


def finalize_platform_stats(
    organizations: list[Organization],
    days: int = 90,
    now: datetime.datetime | None = None,
) -> int:
    """Backfill the cluster-wide ``CostCollectionRun.stats`` fields the
    admin platform-scope report reconciles against
    (``cluster_compute_cost``, ``fixed_overhead``, ``tenant_compute_cost``,
    ``overhead_pool``, ``overhead_allocated``, ``pods_seen``,
    ``pods_unallocated``) on every demo-tagged run in the shared 90-day
    window, across every seeded organization at once.

    ``seed_cost_history`` runs once per organization and (correctly) shares
    one ``CostCollectionRun`` row per hour across every organization — but
    each call only ever wrote *its own* org's independently-scaled numbers
    into that shared row's stats (whichever org got there first), never a
    true cluster-wide total. Call this once, after every organization has
    been seeded, to fix that up: it re-derives `tenant_compute_cost` as the
    real SUM of every organization's compute/backing_service/build line
    items under each shared run (matching the real collector's
    single-pass-covers-everyone semantics), and `cluster_compute_cost` as
    that total marked up by a random per-run idle margin rather than a
    fleet-wide constant. The current `CostNode` fleet is only used for
    `node_hours`.

    Real (non-demo) hours are left untouched. Returns the number of runs
    updated.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.UTC)
    last_hour = now.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(
        hours=1
    )
    first_hour = last_hour - datetime.timedelta(hours=days * 24 - 1)

    rates = effective_rates()

    nodes: list[CostNode] = CostNode.query.filter(
        CostNode.instance_type.isnot(None)
    ).all()
    fixed_overhead = sum(rates.get("fixed_hourly", {}).values())

    # Pod-equivalent count across every organization: replica counts (not
    # distinct process/resource rows), to land in the same ballpark as the
    # real feeder/collector's actual pod count at this scale (~150, not the
    # ~47 distinct process-definition rows `_collect_processes` entries
    # would give if simply counted).
    total_pods = 0
    for organization in organizations:
        for project in organization.active_projects:
            for application in project.active_applications:
                for app_env in application.active_application_environments:
                    counts = app_env.process_counts or application.process_counts or {}
                    total_pods += sum(c for c in counts.values() if c)
        for resource_entry in _collect_resources(organization):
            total_pods += resource_entry.get("instances", 1)

    runs: list[CostCollectionRun] = CostCollectionRun.query.filter(
        CostCollectionRun.period_start >= first_hour,
        CostCollectionRun.period_start <= last_hour,
        CostCollectionRun.status == "succeeded",
    ).all()
    demo_run_ids = [run.id for run in runs if (run.stats or {}).get("seed") == "demo"]
    if not demo_run_ids:
        return 0

    tenant_direct_rows = (
        db.session.query(
            CostLineItem.run_id,
            func.sum(CostLineItem.cost_usd),
        )
        .filter(
            CostLineItem.run_id.in_(demo_run_ids),
            CostLineItem.category.in_(["compute", "backing_service", "build"]),
        )
        .group_by(CostLineItem.run_id)
        .all()
    )
    tenant_direct_by_run: dict[uuid.UUID, float] = {
        run_id: cost for run_id, cost in tenant_direct_rows
    }

    rng = random.Random("platform-stats-finalize")  # nosec B311
    raw_connection = db.session.connection().connection.dbapi_connection
    if raw_connection is None:
        raise RuntimeError("Demo platform stats require a database connection")
    cursor = cast(psycopg.Cursor, raw_connection.cursor())
    patches: list[tuple[Json, uuid.UUID]] = []
    for run_id in demo_run_ids:
        tenant_direct = float(tenant_direct_by_run.get(run_id) or 0.0)
        # cluster_compute must always exceed tenant_direct (it's priced node
        # *capacity*, tenant_direct is actual billed usage of a fraction of
        # it) -- derive it per-hour as tenant_direct marked up by a random
        # 15-25% idle margin rather than a fleet-wide constant, so it tracks
        # tenant_direct's diurnal swings instead of going below it (and thus
        # below requested_cost, which is bounded by tenant_direct) whenever
        # usage spikes above whatever the constant happened to be.
        idle_fraction = rng.uniform(0.15, 0.25)
        cluster_compute_cost = (
            tenant_direct / (1.0 - idle_fraction) if tenant_direct > 0 else 0.0
        )
        overhead_pool = max(cluster_compute_cost - tenant_direct, 0.0) + fixed_overhead
        overhead_allocated = overhead_pool if tenant_direct > 0 else 0.0
        pods_unallocated = max(0, round(total_pods * rng.uniform(0.01, 0.03)))
        patch = Json(
            {
                "cluster_compute_cost": cluster_compute_cost,
                "fixed_overhead": fixed_overhead,
                "tenant_compute_cost": tenant_direct,
                "overhead_pool": overhead_pool,
                "overhead_allocated": overhead_allocated,
                "pods_seen": total_pods,
                "pods_unallocated": pods_unallocated,
                "node_hours": float(len(nodes)),
                "nodes_unpriced": 0,
                "node_data_available": True,
            }
        )
        patches.append((patch, run_id))
    cursor.executemany(
        "UPDATE cost_collection_runs SET stats = stats || %s::jsonb WHERE id = %s",
        patches,
    )
    db.session.commit()
    return len(patches)


# ---------------------------------------------------------------------------
# Giant-scale node fleet & platform workloads (opt-in via SEED_SCALE=giant)
# ---------------------------------------------------------------------------

#: (name, instance_type, capacity_type, cpu_capacity, memory_gib)
_GiantNodeSpec = tuple[str, str, str, float, float]

ACTUALS_DAYS = 10


def _giant_node_fleet(
    rates: CostRates, peak_hourly_cost: float
) -> list[_GiantNodeSpec]:
    """A Karpenter-style Graviton fleet: an always-on base (a general-purpose
    majority, a compute-optimized pool, a couple of spot nodes for memory-
    and burst-heavy work) followed by enough m8g.4xlarge burst nodes to cover
    the busiest hour. `_fleet_node_hours` scales the burst pool in and out
    hour by hour, filling it in list order."""
    fleet: list[_GiantNodeSpec] = []
    for i in range(1, 7):
        fleet.append(
            (f"node-m8g-4xlarge-{i:02d}", "m8g.4xlarge", "on-demand", 16.0, 64.0)
        )
    for i in range(1, 4):
        fleet.append(
            (f"node-c8g-2xlarge-{i:02d}", "c8g.2xlarge", "on-demand", 8.0, 16.0)
        )
    for i in range(1, 3):
        fleet.append((f"node-r8g-2xlarge-{i:02d}", "r8g.2xlarge", "spot", 8.0, 64.0))
    fleet.append(("node-t4g-large-01", "t4g.large", "spot", 2.0, 8.0))
    base_cost = sum(_node_price(spec, rates) for spec in fleet)
    burst_spec = ("", "m8g.4xlarge", "on-demand", 16.0, 64.0)
    burst_count = max(
        0, math.ceil((peak_hourly_cost - base_cost) / _node_price(burst_spec, rates))
    )
    for i in range(1, burst_count + 1):
        fleet.append(
            (f"node-m8g-4xlarge-burst-{i:02d}", "m8g.4xlarge", "on-demand", 16.0, 64.0)
        )
    return fleet


#: How many leading `_giant_node_fleet` entries are the always-on base.
_GIANT_BASE_NODES = 12


def _node_price(spec: _GiantNodeSpec, rates: CostRates) -> float:
    _, instance_type, _, cpu_capacity, memory_gib = spec
    return node_hourly_price(
        instance_type, rates, cpu_capacity=cpu_capacity, memory_gib=memory_gib
    )


def _fleet_node_hours(prices: list[float], target_cost: float) -> list[float]:
    """Node-hours per fleet node for one hour whose priced capacity should
    come to `target_cost`: the base runs the whole hour and burst nodes join
    in order, the last one for part of the hour. An hour cheaper than the
    base alone runs every base node for the same fraction."""
    base_cost = sum(prices[:_GIANT_BASE_NODES])
    if target_cost <= base_cost:
        fraction = target_cost / base_cost if base_cost > 0 else 0.0
        return [fraction] * _GIANT_BASE_NODES + [0.0] * (
            len(prices) - _GIANT_BASE_NODES
        )
    hours = [1.0] * _GIANT_BASE_NODES
    remaining = target_cost - base_cost
    for price in prices[_GIANT_BASE_NODES:]:
        share = min(1.0, remaining / price) if remaining > 0 and price > 0 else 0.0
        hours.append(share)
        remaining -= share * price
    return hours


#: namespace -> workload names, mirroring the real collector's
#: `{namespace}/{workload}` platform-component naming (see
#: collector.py's `_platform_pod_workload`/`_add_platform`).
_GIANT_PLATFORM_NAMESPACES: dict[str, list[str]] = {
    "cabotage": [
        "api",
        "scheduler",
        "operator",
        "webhook",
        "builder",
        "registry-proxy",
        *(f"worker-shard-{i}" for i in range(16)),
    ],
    "kube-system": [
        "coredns",
        "aws-node",
        "kube-proxy",
        "ebs-csi-controller",
        "ebs-csi-node",
        "cluster-autoscaler",
        "metrics-server",
        "karpenter",
    ],
    "redis": [
        "redis-sentinel",
        "redis-proxy",
        "redis-exporter",
        *(f"redis-cluster-{i}" for i in range(10)),
    ],
    "postgres": [
        "postgres-operator",
        "pgbouncer",
        "pg-exporter",
        *(f"postgres-replica-{i}" for i in range(8)),
    ],
    "clickhouse": [
        "clickhouse-operator",
        "clickhouse-keeper",
        "clickhouse-exporter",
        *(f"clickhouse-shard-{i}" for i in range(10)),
    ],
    "monitoring": [
        "prometheus",
        "grafana",
        "alertmanager",
        "resident-mimir-write",
        "resident-mimir-read",
        "loki-write",
        "loki-read",
        "tempo",
        "mimir-compactor",
        "mimir-store-gateway",
        *(f"mimir-ingester-{i}" for i in range(16)),
    ],
    "cert-manager": [
        "cert-manager-controller",
        "cert-manager-webhook",
        "cert-manager-cainjector",
    ],
    "traefik": ["traefik-ingress", "traefik-gateway", "traefik-middleware"],
    "ingress-nginx": ["ingress-nginx-controller", "ingress-nginx-admission"],
    "elastic-system": [
        "elastic-operator",
        "elasticsearch-master",
        "kibana",
        *(f"elasticsearch-data-{i}" for i in range(8)),
    ],
    "default": ["datadog-agent", "datadog-cluster-agent", "datadog-logs-agent"],
    "storage": ["data-minio", "minio-operator"],
}

#: PVC claims built-cache workloads live under either the shared `default`
#: namespace or the dedicated tenant-builds namespace, mirroring real
#: `build-image-cache-<org>-<project>-<app>` claim names from
#: `_collect_pv_storage`.
_GIANT_BUILD_CACHE_NAMESPACES = ("default", "cabotage-tenant-builds")
_GIANT_BUILD_CACHE_COUNT = 60


def _giant_platform_component_names(
    organizations: list[Organization],
) -> list[str]:
    """~200 stable platform-component names: cluster add-ons/infra
    (compute+memory+network) plus per-app build-image-cache PVCs
    (storage-only), mirroring the real collector's naming conventions."""
    names: list[str] = [
        f"{namespace}/{workload}"
        for namespace, workloads in _GIANT_PLATFORM_NAMESPACES.items()
        for workload in workloads
    ]
    names.append("unclaimed/volumes")

    app_keys: list[str] = []
    for organization in sorted(organizations, key=lambda o: o.slug):
        for project in sorted(organization.active_projects, key=lambda p: p.slug):
            for application in sorted(
                project.active_applications, key=lambda a: a.slug
            ):
                app_keys.append(
                    f"{organization.slug}-{project.slug}-{application.slug}"
                )
    for i, key in enumerate(app_keys[:_GIANT_BUILD_CACHE_COUNT]):
        namespace = _GIANT_BUILD_CACHE_NAMESPACES[
            i % len(_GIANT_BUILD_CACHE_NAMESPACES)
        ]
        names.append(f"{namespace}/build-image-cache-{key}")
    return names


def _component_weight(name: str) -> float:
    """Long-tail magnitude: a couple of dominant stateful workloads, a
    handful of sharded/replica pools, a mass of tiny build caches, and
    everything else in between."""
    if "resident-mimir-write" in name or "data-minio" in name:
        return 120.0
    if name.startswith(
        ("default/build-image-cache", "cabotage-tenant-builds/build-image-cache")
    ):
        return 0.4
    if any(
        tag in name
        for tag in ("-shard-", "-ingester-", "-data-", "-replica-", "-cluster-")
    ):
        return 4.0
    return 1.0


def _giant_hour_platform_components(
    names: list[str], rates: CostRates, rng: random.Random, compute_budget: float
) -> dict[str, dict[str, float]]:
    """One hour's worth of `PlatformComponentStats`-shaped values per
    component name, long-tail weighted so totals over any range stay
    dominated by a few stateful workloads. Compute workloads are scaled so
    together they cost `compute_budget`, keeping them inside the share of
    the fleet tenants don't use."""
    components: dict[str, dict[str, float]] = {}
    for name in names:
        weight = _component_weight(name)
        jitter = rng.uniform(0.85, 1.15)
        is_storage_only = "build-image-cache" in name or name in (
            "storage/data-minio",
            "unclaimed/volumes",
        )
        if is_storage_only:
            storage_gib_month = (weight * 8.0 * jitter) / HOURS_PER_MONTH
            components[name] = {
                "cpu_billed_hours": 0.0,
                "memory_billed_gib_hours": 0.0,
                "network_gb": 0.0,
                "storage_gib_month": storage_gib_month,
                "cost_usd": storage_gib_month * rates["storage_gib_month"],
            }
            continue
        cpu_h = weight * 0.4 * jitter
        mem_h = weight * 1.0 * jitter
        net_gb = weight * 0.05 * jitter
        cost = (
            cpu_h * rates["vcpu_hour"]
            + mem_h * rates["memory_gib_hour"]
            + net_gb * rates["network_gb"]
        )
        components[name] = {
            "cpu_billed_hours": cpu_h,
            "memory_billed_gib_hours": mem_h,
            "network_gb": net_gb,
            "storage_gib_month": 0.0,
            "cost_usd": cost,
        }
    compute_cost = sum(
        c["cost_usd"] for c in components.values() if c["cpu_billed_hours"]
    )
    scale = compute_budget / compute_cost if compute_cost > 0 else 0.0
    for component in components.values():
        if component["cpu_billed_hours"]:
            for key in (
                "cpu_billed_hours",
                "memory_billed_gib_hours",
                "network_gb",
                "cost_usd",
            ):
                component[key] *= scale
    return components


def _giant_node_hour_row(
    run_id: uuid.UUID,
    hour: datetime.datetime,
    node: _GiantNodeSpec,
    node_hours: float,
    rates: CostRates,
    rng: random.Random,
) -> tuple[object, ...]:
    """One node's load/price for one collected hour (`node_hours` of it),
    mirroring collector.py's `_collect_nodes`/`_price_node_hour` math over a
    synthetic diurnal+weekly utilization curve."""
    name, instance_type, capacity_type, cpu_capacity, memory_gib = node
    utilization = max(
        0.05,
        min(
            0.95,
            _weekly_factor(hour.weekday())
            * _diurnal_factor(hour.hour)
            * rng.uniform(0.7, 1.1),
        ),
    )
    cpu_usage = cpu_capacity * utilization * node_hours
    cpu_request = (
        cpu_capacity * min(0.95, utilization * rng.uniform(1.0, 1.3)) * node_hours
    )
    cpu_billed = max(cpu_usage, cpu_request)
    mem_usage = memory_gib * utilization * node_hours
    mem_request = (
        memory_gib * min(0.95, utilization * rng.uniform(1.0, 1.3)) * node_hours
    )
    mem_billed = max(mem_usage, mem_request)

    hourly_price = _node_price(node, rates)
    cost = node_hours * hourly_price
    capacity_value = (
        cpu_capacity * rates["vcpu_hour"] + memory_gib * rates["memory_gib_hour"]
    )
    scale = (hourly_price / capacity_value) if capacity_value > 0 else 0.0

    def _value(cpu_hours: float, mem_gib_hours: float) -> float:
        value = scale * (
            cpu_hours * rates["vcpu_hour"] + mem_gib_hours * rates["memory_gib_hour"]
        )
        return min(max(value, 0.0), cost)

    requested_cost = _value(cpu_request, mem_request)
    used_cost = _value(cpu_usage, mem_usage)
    idle_cost = cost - _value(cpu_billed, mem_billed)

    return (
        uuid.uuid4(),
        run_id,
        hour,
        name,
        instance_type,
        capacity_type,
        cpu_capacity,
        memory_gib,
        node_hours,
        cpu_request,
        cpu_usage,
        cpu_billed,
        mem_request,
        mem_usage,
        mem_billed,
        cost,
        requested_cost,
        used_cost,
        idle_cost,
    )


def _seed_aws_actuals(
    compute_by_day: dict[datetime.date, float],
    now: datetime.datetime | None = None,
    days: int = ACTUALS_DAYS,
) -> int:
    """Seed `CostAwsDaily` actuals for the most recent `days` days so the
    admin AWS-bill reconciliation table (``COST_AWS_ACTUALS_ENABLED``) has
    something to compare against the allocated/collected totals. The EC2
    bucket tracks the seeded fleet's priced node-hours per day
    (`compute_by_day`) plus a few percent of what node pricing misses.
    Upserts by the table's own `(day, bucket)` primary key, so re-seeding
    is a cheap no-op refresh rather than an error."""
    now = now or datetime.datetime.now(datetime.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.UTC)
    last_day = (now - datetime.timedelta(days=1)).date()
    first_day = last_day - datetime.timedelta(days=days - 1)

    rng = random.Random("giant-aws-actuals")  # nosec B311
    bucket_bases = {
        "compute": 420.0,
        "storage": 38.0,
        "cross_az": 22.0,
        "internet_egress": 15.0,
        "load_balancer": 9.0,
        "inter_region": 4.0,
    }
    rows: list[tuple[datetime.date, str, float, datetime.datetime]] = []
    day = first_day
    while day <= last_day:
        weekday_factor = _weekly_factor(day.weekday())
        for bucket, base in bucket_bases.items():
            if bucket == "compute" and day in compute_by_day:
                cost = round(compute_by_day[day] * rng.uniform(1.01, 1.06), 6)
            else:
                cost = round(base * weekday_factor * rng.uniform(0.9, 1.1), 6)
            rows.append((day, bucket, cost, now))
        day += datetime.timedelta(days=1)

    raw_connection = db.session.connection().connection.dbapi_connection
    if raw_connection is None:
        raise RuntimeError("Demo AWS actuals seeding requires a database connection")
    cursor = cast(psycopg.Cursor, raw_connection.cursor())
    cursor.executemany(
        "INSERT INTO cost_aws_daily (day, bucket, cost_usd, fetched_at) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (day, bucket) DO UPDATE SET "
        "cost_usd = EXCLUDED.cost_usd, fetched_at = EXCLUDED.fetched_at",
        rows,
    )
    db.session.commit()
    return len(rows)


def seed_cost_nodes_and_platform(
    organizations: list[Organization],
    days: int = 90,
    now: datetime.datetime | None = None,
) -> dict[str, int]:
    """Seed a synthetic `CostNode` fleet, `CostNodeHour` load for every
    demo hour, `stats['platform_components']` workloads on every demo
    run, and recent `CostAwsDaily` actuals -- the three data sources the
    real collector writes that `seed_cost_history`/`finalize_platform_stats`
    never touch -- so the admin platform-scope report's Nodes and Platform
    workloads tables, and the AWS-bill reconciliation, are populated at
    SEED_SCALE=giant.

    The numbers agree with each other the way collector output does: each
    hour's fleet is sized to the `cluster_compute_cost`
    `finalize_platform_stats` derived from tenant usage, then that stat is
    rewritten as the sum of the hour's node-hour costs (collector.py does
    the same). Platform workloads fill part of the capacity tenants leave
    unused, and the AWS EC2 bucket tracks the node-hour cost per day.

    Cluster-wide like `finalize_platform_stats`: call once after every
    organization has been seeded, never per-organization. Idempotent: node
    rows are upserted by name, `CostNodeHour` rows for demo runs are
    cleared and rewritten every call (respecting `UNIQUE(run_id,
    node_name)`), and the stats patch always replaces the same keys via the
    same `stats || jsonb` merge `finalize_platform_stats` uses.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.UTC)
    last_hour = now.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(
        hours=1
    )
    first_hour = last_hour - datetime.timedelta(hours=days * 24 - 1)

    rates = effective_rates()
    runs: list[CostCollectionRun] = CostCollectionRun.query.filter(
        CostCollectionRun.period_start >= first_hour,
        CostCollectionRun.period_start <= last_hour,
        CostCollectionRun.status == "succeeded",
    ).all()
    demo_runs = [run for run in runs if (run.stats or {}).get("seed") == "demo"]
    targets = {
        run.id: float((run.stats or {}).get("cluster_compute_cost") or 0.0)
        for run in demo_runs
    }
    fleet = _giant_node_fleet(rates, max(targets.values(), default=0.0))
    prices = [_node_price(spec, rates) for spec in fleet]

    existing_nodes: dict[str, CostNode] = {
        node.name: node
        for node in CostNode.query.filter(
            CostNode.name.in_([spec[0] for spec in fleet])
        ).all()
    }
    now_ts = datetime.datetime.now(datetime.UTC)
    for name, instance_type, capacity_type, cpu_capacity, memory_gib in fleet:
        node = existing_nodes.get(name)
        if node is None:
            node = CostNode(name=name)
            db.session.add(node)
        node.instance_type = instance_type
        node.capacity_type = capacity_type
        node.cpu_capacity = cpu_capacity
        node.memory_gib = memory_gib
        node.last_seen = now_ts
    db.session.flush()
    db.session.commit()

    if not demo_runs:
        return {
            "nodes": len(fleet),
            "node_hours": 0,
            "components": 0,
            "runs_patched": 0,
            "aws_actuals_rows": _seed_aws_actuals({}, now=now),
        }
    demo_run_ids = [run.id for run in demo_runs]

    CostNodeHour.query.filter(CostNodeHour.run_id.in_(demo_run_ids)).delete(
        synchronize_session=False
    )
    db.session.flush()

    component_names = _giant_platform_component_names(organizations)

    raw_connection = db.session.connection().connection.dbapi_connection
    if raw_connection is None:
        raise RuntimeError("Demo node/platform seeding requires a database connection")
    cursor = cast(psycopg.Cursor, raw_connection.cursor())

    node_hour_rows: list[tuple[object, ...]] = []
    patches: list[tuple[Json, uuid.UUID]] = []
    compute_by_day: dict[datetime.date, float] = {}
    for run in demo_runs:
        hour = run.period_start
        stats = run.stats or {}
        hour_rng = random.Random(f"giant-demo-hour-{hour.isoformat()}")  # nosec B311
        node_hours = _fleet_node_hours(prices, targets[run.id])
        cluster_compute_cost = 0.0
        for node, hours in zip(fleet, node_hours, strict=True):
            if hours <= 0:
                continue
            row = _giant_node_hour_row(run.id, hour, node, hours, rates, hour_rng)
            node_hour_rows.append(row)
            cluster_compute_cost += hours * _node_price(node, rates)
        day = hour.date()
        compute_by_day[day] = compute_by_day.get(day, 0.0) + cluster_compute_cost

        tenant_direct = float(stats.get("tenant_compute_cost") or 0.0)
        fixed_overhead = float(stats.get("fixed_overhead") or 0.0)
        unused = max(cluster_compute_cost - tenant_direct, 0.0)
        overhead_pool = unused + fixed_overhead
        components = _giant_hour_platform_components(
            component_names, rates, hour_rng, unused * hour_rng.uniform(0.5, 0.7)
        )
        patch = {
            "platform_components": components,
            "cluster_compute_cost": cluster_compute_cost,
            "overhead_pool": overhead_pool,
            "overhead_allocated": overhead_pool if tenant_direct > 0 else 0.0,
            "node_hours": sum(node_hours),
        }
        patches.append((Json(patch), run.id))

    _copy_rows(cursor, "cost_node_hours", NODE_HOUR_COLUMNS, node_hour_rows)
    cursor.executemany(
        "UPDATE cost_collection_runs SET stats = stats || %s::jsonb WHERE id = %s",
        patches,
    )
    db.session.commit()
    aws_actuals_rows = _seed_aws_actuals(compute_by_day, now=now)

    return {
        "nodes": len(fleet),
        "node_hours": len(node_hour_rows),
        "components": len(component_names),
        "runs_patched": len(patches),
        "aws_actuals_rows": aws_actuals_rows,
    }


# ---------------------------------------------------------------------------
# Source data
# ---------------------------------------------------------------------------


def _collect_processes(
    organization: Organization,
) -> tuple[list[ProcessEntry], list[Application]]:
    """One entry per (app_env, process): request vCPU/GiB, scope ids.

    Soft-deleted projects, apps and environments are included with
    ``gone_after`` set, so their history stays seeded up to deletion. Also
    returns the active Application rows, for picking one to soft-delete as a
    demo narrative beat.
    """
    entries: list[ProcessEntry] = []
    applications: list[Application] = []
    for project in organization.projects:
        for application in project.project_applications:
            if project.deleted_at is None and application.deleted_at is None:
                applications.append(application)
            for app_env in application.application_environments:
                deletions = [
                    as_utc(d)
                    for d in (
                        project.deleted_at,
                        application.deleted_at,
                        app_env.deleted_at,
                        app_env.environment.deleted_at,
                    )
                    if d is not None
                ]
                counts: dict[str, int] = (
                    app_env.process_counts or application.process_counts or {}
                )
                pod_class_map: dict[str, str] = (
                    app_env.process_pod_classes or application.process_pod_classes or {}
                )
                for process_name, count in counts.items():
                    if not count:
                        continue
                    pod_class_name = pod_class_map.get(process_name, DEFAULT_POD_CLASS)
                    pod_class = pod_classes.get(
                        pod_class_name, pod_classes[DEFAULT_POD_CLASS]
                    )
                    cpu_request_raw = parse_cpu_quantity(pod_class["cpu"]["requests"])
                    mem_request_raw = parse_memory_quantity_gib(
                        pod_class["memory"]["requests"]
                    )
                    cpu_request = (cpu_request_raw or 0.0) * count
                    mem_request = (mem_request_raw or 0.0) * count
                    entries.append(
                        {
                            "organization_id": organization.id,
                            "project_id": project.id,
                            "environment_id": app_env.environment_id,
                            "application_id": application.id,
                            "component": process_name,
                            "cpu_request": cpu_request,
                            "mem_request": mem_request,
                            "is_web": process_name == "web",
                            "appears_after": None,
                            "gone_after": min(deletions, default=None),
                        }
                    )
    return entries, applications


def _collect_resources(organization: Organization) -> list[ResourceEntry]:
    entries: list[ResourceEntry] = []
    for project in organization.active_projects:
        for environment in project.active_environments:
            for resource in environment.active_resources:
                # Same instance counts as the collector's storage pricing.
                if resource.type == "postgres":
                    instances = 2 if resource.ha_enabled else 1
                elif (
                    resource.type == "redis"
                    and isinstance(resource, RedisResource)
                    and resource.ha_enabled
                ):
                    instances = (resource.leader_replicas or 0) + (
                        resource.follower_replicas or 0
                    )
                else:
                    instances = 1
                cpu_request, mem_request = _resource_requests(resource)
                entries.append(
                    {
                        "organization_id": organization.id,
                        "project_id": project.id,
                        "environment_id": environment.id,
                        "resource_id": resource.id,
                        "slug": resource.slug,
                        "storage_gib": resource.storage_size * instances,
                        "cpu_request": cpu_request * instances,
                        "mem_request": mem_request * instances,
                        "instances": instances,
                    }
                )
    return entries


_RESOURCE_SIZE_CLASSES = {
    "postgres": postgres_size_classes,
    "redis": redis_size_classes,
}


def _resource_requests(resource: Resource) -> tuple[float, float]:
    """Per-instance vCPU/GiB requests for a backing service's size class."""
    classes = _RESOURCE_SIZE_CLASSES.get(resource.type or "", {})
    size = classes.get(resource.size_class)
    if size is None:
        return 0.0, 0.0
    cpu = parse_cpu_quantity(size["cpu"]["requests"]) or 0.0
    memory = parse_memory_quantity_gib(size["memory"]["requests"]) or 0.0
    return cpu, memory


# ---------------------------------------------------------------------------
# Per-hour generation
# ---------------------------------------------------------------------------


def _distinct_app_scopes(processes: list[ProcessEntry]) -> list[ScopeKey]:
    """Unique (org, project, environment, application, None) scope keys."""
    seen: dict[ScopeKey, bool] = {}
    for proc in processes:
        key: ScopeKey = (
            proc["organization_id"],
            proc["project_id"],
            proc["environment_id"],
            proc["application_id"],
            None,
        )
        seen[key] = True
    return list(seen)


def _weekly_factor(weekday: int) -> float:
    """Weekday traffic runs higher than weekends (weekday(): Mon=0..Sun=6)."""
    return 1.0 if weekday < 5 else 0.6


def _diurnal_factor(hour_of_day: int) -> float:
    """Smooth business-hours curve peaking at 15:00 UTC, in [0.3, 0.8] --
    matches the synthetic feeder's own diurnal envelope (feeder.py's
    gen_cpu_mem_net_series) exactly, since this is the usage-fraction curve
    real collected data from that feeder actually produces.
    """
    phase = (hour_of_day - 15) / 24.0 * 2 * math.pi
    return 0.3 + 0.5 * (0.5 + 0.5 * math.cos(phase))


def _usage_for(
    request_quantity: float,
    diurnal: float,
    rng: random.Random,
    *,
    burst_chance: float = 0.05,
) -> float:
    """Mirrors feeder.py's gen_cpu_mem_net_series per-sample fraction
    formula exactly (frac = diurnal * noise, occasionally replaced by a
    burst fraction) so demo-seeded tenant_direct cost lands in the same
    ballpark as what the real feeder + collector produce for the same
    entities, instead of usage regularly exceeding request at peak hours.
    """
    frac = diurnal * rng.uniform(0.85, 1.15)
    if rng.random() < burst_chance:
        frac = rng.uniform(0.9, 1.3)
    frac = max(0.05, frac)
    return request_quantity * frac


def _generate_hour(
    hour: datetime.datetime,
    processes: list[ProcessEntry],
    resources: list[ResourceEntry],
    rates: CostRates,
    rng: random.Random,
    events: Events,
) -> tuple[list[CostLineItem], CostRunStats]:
    items: list[CostLineItem] = []
    diurnal = _diurnal_factor(hour.hour) * _weekly_factor(hour.weekday())
    is_spike_hour = hour.date() == events["spike_date"] and 8 <= hour.hour <= 20
    scaled_up = hour >= events["scale_up_cutoff"]

    direct_compute_total = 0.0
    direct_weight_by_scope: dict[ScopeKey, float] = {}

    def _track_direct(scope_key: ScopeKey, cost: float) -> None:
        direct_weight_by_scope[scope_key] = (
            direct_weight_by_scope.get(scope_key, 0.0) + cost
        )

    live = [
        proc
        for proc in processes
        if (proc["appears_after"] is None or hour >= proc["appears_after"])
        and (proc["gone_after"] is None or hour < proc["gone_after"])
    ]
    for proc in live:
        # Fleet-wide scale-up ~30 days ago: recent/current hours use the
        # organization's actual configured pod class/replica counts (no
        # multiplier) so they match what the real collector reads from the
        # same entities today; only the OLDER pre-cutoff history is scaled
        # down to tell a "the org grew over time" story, instead of scaling
        # recent data UP past what's real.
        request_scale = 1.0 if scaled_up else (1.0 / 1.4)
        cpu_request = proc["cpu_request"] * request_scale
        mem_request = proc["mem_request"] * request_scale

        # A one-day traffic spike drives web usage (and its egress) up.
        usage_scale = 2.5 if (is_spike_hour and proc["is_web"]) else 1.0
        cpu_usage = _usage_for(cpu_request, diurnal, rng) * usage_scale
        mem_usage = _usage_for(mem_request, diurnal, rng) * usage_scale
        cpu_billed = max(cpu_request, cpu_usage)
        mem_billed = max(mem_request, mem_usage)
        cpu_cost = cpu_billed * rates["vcpu_hour"]
        mem_cost = mem_billed * rates["memory_gib_hour"]

        scope_key: ScopeKey = (
            proc["organization_id"],
            proc["project_id"],
            proc["environment_id"],
            proc["application_id"],
            None,
        )
        items.append(
            CostLineItem(
                organization_id=proc["organization_id"],
                project_id=proc["project_id"],
                environment_id=proc["environment_id"],
                application_id=proc["application_id"],
                resource_id=None,
                category="compute",
                meter="vcpu",
                component=proc["component"],
                quantity=cpu_billed,
                unit="vCPU-hr",
                usage_quantity=cpu_usage,
                request_quantity=cpu_request,
                cost_usd=cpu_cost,
            )
        )
        items.append(
            CostLineItem(
                organization_id=proc["organization_id"],
                project_id=proc["project_id"],
                environment_id=proc["environment_id"],
                application_id=proc["application_id"],
                resource_id=None,
                category="compute",
                meter="memory",
                component=proc["component"],
                quantity=mem_billed,
                unit="GiB-hr",
                usage_quantity=mem_usage,
                request_quantity=mem_request,
                cost_usd=mem_cost,
            )
        )
        direct_compute_total += cpu_cost + mem_cost
        _track_direct(scope_key, cpu_cost + mem_cost)

        if proc["is_web"]:
            traffic = (cpu_usage / cpu_request) if cpu_request else diurnal
            gb = max(traffic, 0.05) * rng.uniform(0.04, 0.12) * usage_scale
            egress_cost = gb * rates["egress_gb"]
            items.append(
                CostLineItem(
                    organization_id=proc["organization_id"],
                    project_id=proc["project_id"],
                    environment_id=proc["environment_id"],
                    application_id=proc["application_id"],
                    resource_id=None,
                    category="egress",
                    meter="egress",
                    component=proc["component"],
                    quantity=gb,
                    unit="GB",
                    usage_quantity=None,
                    request_quantity=None,
                    cost_usd=egress_cost,
                )
            )

    for resource in resources:
        cpu_usage = _usage_for(resource["cpu_request"], diurnal, rng, burst_chance=0.03)
        mem_usage = _usage_for(resource["mem_request"], diurnal, rng, burst_chance=0.03)
        cpu_billed = max(resource["cpu_request"], cpu_usage)
        mem_billed = max(resource["mem_request"], mem_usage)
        cpu_cost = cpu_billed * rates["vcpu_hour"]
        mem_cost = mem_billed * rates["memory_gib_hour"]

        scope_key = (
            resource["organization_id"],
            resource["project_id"],
            resource["environment_id"],
            None,
            resource["resource_id"],
        )
        items.append(
            CostLineItem(
                organization_id=resource["organization_id"],
                project_id=resource["project_id"],
                environment_id=resource["environment_id"],
                application_id=None,
                resource_id=resource["resource_id"],
                category="backing_service",
                meter="vcpu",
                component=resource["slug"],
                quantity=cpu_billed,
                unit="vCPU-hr",
                usage_quantity=cpu_usage,
                request_quantity=resource["cpu_request"],
                cost_usd=cpu_cost,
            )
        )
        items.append(
            CostLineItem(
                organization_id=resource["organization_id"],
                project_id=resource["project_id"],
                environment_id=resource["environment_id"],
                application_id=None,
                resource_id=resource["resource_id"],
                category="backing_service",
                meter="memory",
                component=resource["slug"],
                quantity=mem_billed,
                unit="GiB-hr",
                usage_quantity=mem_usage,
                request_quantity=resource["mem_request"],
                cost_usd=mem_cost,
            )
        )
        direct_compute_total += cpu_cost + mem_cost
        _track_direct(scope_key, cpu_cost + mem_cost)

        storage_quantity = resource["storage_gib"] / HOURS_PER_MONTH
        storage_cost = storage_quantity * rates["storage_gib_month"]
        items.append(
            CostLineItem(
                organization_id=resource["organization_id"],
                project_id=resource["project_id"],
                environment_id=resource["environment_id"],
                application_id=None,
                resource_id=resource["resource_id"],
                category="storage",
                meter="storage",
                component=resource["slug"],
                quantity=storage_quantity,
                unit="GiB-mo",
                usage_quantity=None,
                request_quantity=None,
                cost_usd=storage_cost,
            )
        )

    # Build activity: each active app independently gets a chance of a
    # build during business hours, landing at roughly several builds/week
    # per app, 3-8 minutes each at a realistic buildkit job size.
    if hour.hour in DEMO_BUILD_HOURS:
        for scope_key in _distinct_app_scopes(live):
            if rng.random() >= DEMO_BUILD_HOUR_PROBABILITY:
                continue
            org_id, project_id, environment_id, application_id, _resource_id = scope_key
            build_minutes = rng.uniform(3.0, 8.0)
            build_hours = build_minutes / 60.0
            cpu_billed = DEMO_BUILD_CPU_REQUEST * build_hours
            mem_billed = DEMO_BUILD_MEM_REQUEST * build_hours
            cpu_cost = cpu_billed * rates["vcpu_hour"]
            mem_cost = mem_billed * rates["memory_gib_hour"]
            items.append(
                CostLineItem(
                    organization_id=org_id,
                    project_id=project_id,
                    environment_id=environment_id,
                    application_id=application_id,
                    resource_id=None,
                    category="build",
                    meter="vcpu",
                    component="build",
                    quantity=cpu_billed,
                    unit="vCPU-hr",
                    usage_quantity=cpu_billed,
                    request_quantity=cpu_billed,
                    cost_usd=cpu_cost,
                )
            )
            items.append(
                CostLineItem(
                    organization_id=org_id,
                    project_id=project_id,
                    environment_id=environment_id,
                    application_id=application_id,
                    resource_id=None,
                    category="build",
                    meter="memory",
                    component="build",
                    quantity=mem_billed,
                    unit="GiB-hr",
                    usage_quantity=mem_billed,
                    request_quantity=mem_billed,
                    cost_usd=mem_cost,
                )
            )
            direct_compute_total += cpu_cost + mem_cost
            _track_direct(scope_key, cpu_cost + mem_cost)

    # Shared overhead: a plausible share of direct compute, split
    # proportionally the same way the real collector does.
    # This demo org represents a modest slice of a shared dev cluster: give
    # it a fraction of the cluster-wide fixed overhead rather than the whole
    # thing, plus a small proportional share of its own direct compute.
    fixed_overhead = sum(rates.get("fixed_hourly", {}).values()) * 0.15
    overhead_pool = direct_compute_total * rng.uniform(0.02, 0.04) + fixed_overhead
    total_weight = sum(direct_weight_by_scope.values())
    if total_weight > 0:
        for scope_key, weight in direct_weight_by_scope.items():
            org_id, project_id, environment_id, application_id, resource_id = scope_key
            share = weight / total_weight
            cost = overhead_pool * share
            items.append(
                CostLineItem(
                    organization_id=org_id,
                    project_id=project_id,
                    environment_id=environment_id,
                    application_id=application_id,
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

    stats: CostRunStats = {
        "pods_seen": len(processes),
        "pods_unallocated": 0,
        "tenant_compute_cost": direct_compute_total,
        "overhead_pool": overhead_pool,
        "fixed_overhead": fixed_overhead,
        "synthetic": True,
    }
    return items, stats
