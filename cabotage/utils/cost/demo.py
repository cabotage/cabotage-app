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
from typing import TypedDict, cast

import psycopg
from psycopg import sql as pg_sql
from psycopg.types.json import Json
from sqlalchemy import func

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.cost import CostCollectionRun, CostLineItem, CostNode
from cabotage.server.models.projects import (
    DEFAULT_POD_CLASS,
    Application,
    pod_classes,
)
from cabotage.server.models.resources import RedisResource
from cabotage.utils.cost.collector import (
    ScopeKey,
    StatValue,
    _parse_cpu_quantity,
    _parse_memory_quantity_gib,
)
from cabotage.utils.cost.rates import CostRates, effective_rates

_RUN_COLUMNS = (
    "id",
    "period_start",
    "status",
    "started_at",
    "finished_at",
    "error",
    "rates",
    "stats",
)
_LINE_ITEM_COLUMNS = (
    "id",
    "run_id",
    "period_start",
    "organization_id",
    "project_id",
    "environment_id",
    "application_id",
    "resource_id",
    "category",
    "meter",
    "component",
    "quantity",
    "unit",
    "usage_quantity",
    "request_quantity",
    "cost_usd",
)


class ProcessEntry(TypedDict):
    organization_id: uuid.UUID
    project_id: uuid.UUID
    environment_id: uuid.UUID
    application_id: uuid.UUID
    component: str
    cpu_request: float
    mem_request: float
    is_web: bool
    appears_after: datetime.datetime | None


class ResourceEntry(TypedDict):
    organization_id: uuid.UUID
    project_id: uuid.UUID
    environment_id: uuid.UUID
    resource_id: uuid.UUID
    slug: str
    storage_gib: float
    cpu_request: float
    mem_request: float
    instances: int


class Events(TypedDict):
    spike_date: datetime.date
    scale_up_cutoff: datetime.datetime


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

    # Clear this organization's own CostLineItem rows that fall outside the
    # window we're about to (re)write, e.g. leftovers from a previous call
    # with a larger `days`. Scoped to CostLineItem only (not
    # CostCollectionRun, which is cluster-wide and shared across orgs).
    CostLineItem.query.filter(
        CostLineItem.organization_id == organization.id,
        CostLineItem.period_start < first_hour,
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
    if len(applications) > 2:

        def _app_weight(a: Application) -> int:
            return sum(
                sum((ae.process_counts or {}).values())
                for ae in a.active_application_environments
            )

        candidates = sorted(applications, key=_app_weight)[:-1]
        if candidates:
            decommissioned = rng.choice(candidates)
            if decommissioned.deleted_at is None:
                decommissioned.deleted_at = now - datetime.timedelta(days=2)
                db.session.add(decommissioned)

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
    _copy_rows(cursor, "cost_collection_runs", _RUN_COLUMNS, run_rows)
    _copy_rows(cursor, "cost_line_items", _LINE_ITEM_COLUMNS, item_rows)
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
    been seeded, to fix that up: it re-derives `cluster_compute_cost`
    directly from the current `CostNode` fleet (constant across every
    seeded hour — demo data doesn't model node scale-up/down over time)
    and `tenant_compute_cost` as the real SUM of every organization's
    compute/backing_service/build line items under each shared run,
    matching the real collector's single-pass-covers-everyone semantics.

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
# Source data
# ---------------------------------------------------------------------------


def _collect_processes(
    organization: Organization,
) -> tuple[list[ProcessEntry], list[Application]]:
    """One entry per (app_env, process): request vCPU/GiB, scope ids.

    Also returns the distinct Application rows seen, for picking one to
    soft-delete as a demo narrative beat.
    """
    entries: list[ProcessEntry] = []
    applications: list[Application] = []
    for project in organization.active_projects:
        for application in project.active_applications:
            applications.append(application)
            for app_env in application.active_application_environments:
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
                    cpu_request_raw = _parse_cpu_quantity(pod_class["cpu"]["requests"])
                    mem_request_raw = _parse_memory_quantity_gib(
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
                        }
                    )
    return entries, applications


def _collect_resources(organization: Organization) -> list[ResourceEntry]:
    entries: list[ResourceEntry] = []
    for project in organization.active_projects:
        for environment in project.active_environments:
            for resource in environment.active_resources:
                if resource.type == "postgres":
                    instances = 2 if resource.ha_enabled else 1
                elif resource.type == "redis" and isinstance(resource, RedisResource):
                    instances = (resource.leader_replicas or 0) + (
                        resource.follower_replicas or 0
                    )
                else:
                    instances = 1
                entries.append(
                    {
                        "organization_id": organization.id,
                        "project_id": project.id,
                        "environment_id": environment.id,
                        "resource_id": resource.id,
                        "slug": resource.slug,
                        "storage_gib": resource.storage_size * instances,
                        "cpu_request": _resource_compute_request(
                            resource.size_class, "cpu"
                        ),
                        "mem_request": _resource_compute_request(
                            resource.size_class, "memory"
                        ),
                        "instances": instances,
                    }
                )
    return entries


def _resource_compute_request(size_class: str | None, resource: str) -> float:
    """Rough vCPU/GiB request for a backing-service size class, for demo realism."""
    # Backing service size classes aren't priced the same way as pod_classes;
    # approximate with a modest fixed footprint scaled by size-class ordinal.
    tiers = ["small", "medium", "large", "xlarge", "2xlarge"]
    ordinal = 1
    for i, tier in enumerate(tiers):
        if size_class and size_class.endswith(tier):
            ordinal = i + 1
            break
    if resource == "cpu":
        return 0.25 * ordinal
    return 0.5 * ordinal


# ---------------------------------------------------------------------------
# Per-hour generation
# ---------------------------------------------------------------------------


# Candidate hours for a build to kick off, and the per-(app, hour) chance of
# one happening — tuned so an active app sees roughly 3-5 builds/week.
_BUILD_HOURS = (9, 10, 11, 13, 14, 16, 20)
_BUILD_HOUR_PROBABILITY = 0.15
_BUILD_CPU_REQUEST = 4.0  # vCPU, a realistic buildkit job size
_BUILD_MEM_REQUEST = 6.0  # GiB


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
) -> tuple[list[CostLineItem], dict[str, StatValue]]:
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

    for proc in processes:
        appears_after = proc["appears_after"]
        if appears_after is not None and hour < appears_after:
            continue

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

        storage_quantity = resource["storage_gib"] / 730.0
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
    if hour.hour in _BUILD_HOURS:
        for scope_key in _distinct_app_scopes(processes):
            if rng.random() >= _BUILD_HOUR_PROBABILITY:
                continue
            org_id, project_id, environment_id, application_id, _resource_id = scope_key
            build_minutes = rng.uniform(3.0, 8.0)
            build_hours = build_minutes / 60.0
            cpu_billed = _BUILD_CPU_REQUEST * build_hours
            mem_billed = _BUILD_MEM_REQUEST * build_hours
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

    stats: dict[str, StatValue] = {
        "pods_seen": len(processes),
        "pods_unallocated": 0,
        "tenant_compute_cost": direct_compute_total,
        "overhead_pool": overhead_pool,
        "fixed_overhead": fixed_overhead,
        "synthetic": True,
    }
    return items, stats
