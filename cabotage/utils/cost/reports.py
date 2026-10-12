"""Cost report assembly — the UI's data contract.

``build_report(scope, range_key, now=None, start=None, end=None)`` aggregates
``CostLineItem`` rows (already priced and categorized by the collector) into
the JSON shape the cost dashboard renders. Pages read these snapshots only;
this module never queries Mimir or Kubernetes.
"""

import datetime
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict, cast

from flask import current_app
from sqlalchemy import Float, Integer, case, func
from sqlalchemy import cast as sa_cast
from sqlalchemy.orm import InstrumentedAttribute, Query

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.cost import (
    CostCollectionRun,
    CostLineItem,
    CostNode,
    CostRunStats,
)
from cabotage.server.models.projects import Application, Environment, Project
from cabotage.server.models.resources import Resource
from cabotage.utils.cost.rates import CostRates, effective_rates, node_hourly_price

type Interval = Literal["hour", "day", "week"]


class MeterRow(TypedDict):
    key: str
    label: str
    unit: str
    quantity: float
    rate: float | None
    cost: float


class BreakdownSlugs(TypedDict, total=False):
    project: str | None
    environment: str | None
    application: str | None
    organization: str | None


class BreakdownRow(TypedDict):
    key: str
    name: str
    context: str | None
    cost: float
    previous_cost: float
    share: float
    categories: dict[str, float]
    efficiency: float | None
    slugs: BreakdownSlugs
    deleted: bool
    meters: list[MeterRow] | None
    resource_series: dict[str, list[float]] | None


class MoverRow(TypedDict):
    key: str
    name: str
    context: str | None
    cost: float
    previous_cost: float
    change: float
    change_pct: float | None
    slugs: BreakdownSlugs


class SeriesBreakdownEntry(TypedDict):
    keys: list[str]
    names: dict[str, str]
    values: dict[str, list[float]]
    other: list[float]


class EfficiencySeries(TypedDict):
    usage: list[float]
    request: list[float]
    billed: list[float]


class SeriesPayload(TypedDict):
    timestamps: list[int]
    categories: dict[str, list[float]]
    previous: NotRequired[list[float]]
    efficiency: NotRequired[EfficiencySeries]
    breakdowns: NotRequired[dict[str, SeriesBreakdownEntry]]


class CapacitySeries(TypedDict):
    capacity_cost: list[float]
    requested_cost: list[float]
    used_cost: list[float]


class ReconciliationStats(TypedDict):
    cluster_compute: float
    fixed_overhead: float
    tenant_direct: float
    overhead_allocated: float
    unallocated_idle: float
    total: float
    residual: float


class NodeRow(TypedDict):
    instance_type: str | None
    capacity_type: str | None
    count: int
    node_hours: float
    hourly: float
    cost: float


class UnallocatedStats(TypedDict):
    pods_seen: float
    pods_unallocated: float
    share: float | None


class PlatformBlock(TypedDict):
    reconciliation: ReconciliationStats
    capacity_series: CapacitySeries
    nodes: list[NodeRow]
    unallocated: UnallocatedStats
    top_orgs_series: SeriesBreakdownEntry


class RateLineItem(TypedDict):
    key: str
    label: str
    value: float | None
    unit: str
    note: str | None


class RatesNode(TypedDict):
    instance_type: str
    hourly: float


class RatesDisplay(TypedDict):
    line_items: list[RateLineItem]
    nodes: list[RatesNode]


class FreshnessStats(TypedDict):
    collection_enabled: bool
    last_run_at: str | None
    data_through: str | None
    stale: bool
    missing_hours: int
    failed_hours: list[str]
    currently_failing: bool
    failing_since: str | None


#: A breakdown's raw group-by key: a scope id, or a component/process name.
type BreakdownKey = uuid.UUID | str

#: What a breakdown's name-resolver callback returns for one raw group-by
#: key: (display name, context line, link slugs, soft-deleted?); ``None``
#: lets a resolver skip a key entirely (no current resolver does, but
#: ``_build_rows`` handles it defensively).
type ResolvedName = tuple[str, str | None, BreakdownSlugs, bool]
type NameResolver = Callable[[BreakdownKey], ResolvedName | None]

#: (aggregation column, optional category filter) per breakdown name.
type BreakdownDimension = tuple[
    InstrumentedAttribute[BreakdownKey | None], list[str] | None
]

#: The 5-tuple ``_aggregate_breakdown`` hands to ``_build_rows``.
type BreakdownAggregates = tuple[
    dict[BreakdownKey, float],
    dict[BreakdownKey, dict[str, float]],
    dict[BreakdownKey, float],
    dict[BreakdownKey, float],
    dict[BreakdownKey, float],
]

type BreakdownFunc = Callable[
    [
        CostScope,
        datetime.datetime,
        datetime.datetime,
        datetime.datetime,
        datetime.datetime,
        CostRates,
    ],
    tuple[list[BreakdownRow], list[BreakdownKey]],
]

CATEGORY_ORDER = [
    "compute",
    "backing_service",
    "storage",
    "egress",
    "build",
    "overhead",
]
CATEGORY_LABELS = {
    "compute": "Compute",
    "backing_service": "Backing services",
    "storage": "Storage",
    "egress": "Egress",
    "build": "Builds",
    "overhead": "Shared overhead",
}
METER_ORDER = ["vcpu", "memory", "storage", "egress", "shared"]
METER_LABELS = {
    "vcpu": "vCPU",
    "memory": "Memory",
    "storage": "Storage",
    "egress": "Egress",
    "shared": "Shared",
}
METER_UNITS = {
    "vcpu": "vCPU-hr",
    "memory": "GiB-hr",
    "storage": "GiB-mo",
    "egress": "GB",
    "shared": "USD",
}
METER_RATE_KEY = {
    "vcpu": "vcpu_hour",
    "memory": "memory_gib_hour",
    "storage": "storage_gib_month",
    "egress": "egress_gb",
}


def _simple_rates(rates: CostRates) -> dict[str, float]:
    """The 4 per-unit rate keys as a plain dict, so callers can look one up
    by a dynamic (non-literal) key without indexing the ``CostRates``
    TypedDict directly -- TypedDict subscripting requires a literal key.
    """
    return {
        "vcpu_hour": rates["vcpu_hour"],
        "memory_gib_hour": rates["memory_gib_hour"],
        "storage_gib_month": rates["storage_gib_month"],
        "egress_gb": rates["egress_gb"],
    }


def _rate_for_meter(rates: CostRates, meter: str) -> float:
    """The per-unit rate for a meter known to have one (vcpu/memory)."""
    return _simple_rates(rates)[METER_RATE_KEY[meter]]


def _rate_for_meter_optional(rates: CostRates, meter: str) -> float | None:
    """The per-unit rate for any meter, or None if it has no rate (e.g.
    the synthetic "shared" overhead meter, which is priced directly in
    dollars rather than via a per-unit rate).
    """
    rate_key = METER_RATE_KEY.get(meter)
    return _simple_rates(rates).get(rate_key) if rate_key else None


class _RangeConfig(TypedDict):
    duration: datetime.timedelta
    interval: Interval
    label: str


# "24h" and "7d" chart hourly (matching the collector's native grain); "30d"
# and "90d" chart daily to keep the series readable. "mtd" is handled
# separately since its length changes through the month.
RANGE_CONFIG: dict[str, _RangeConfig] = {
    "24h": {
        "duration": datetime.timedelta(hours=24),
        "interval": "hour",
        "label": "Last 24 hours",
    },
    "7d": {
        "duration": datetime.timedelta(days=7),
        "interval": "hour",
        "label": "Last 7 days",
    },
    "30d": {
        "duration": datetime.timedelta(days=30),
        "interval": "day",
        "label": "Last 30 days",
    },
    "90d": {
        "duration": datetime.timedelta(days=90),
        "interval": "day",
        "label": "Last 90 days",
    },
}

STALE_AFTER = datetime.timedelta(hours=2)

# The beat task collects the previous hour at minute=7; give it a few extra
# minutes to finish before treating that hour as "missing" in freshness
# reporting.
COLLECTION_GRACE_MINUTES = 10

# Bound on how far back `_freshness` walks hour-by-hour to find the start of
# an ongoing collection-failure streak, so a years-long outage can't turn
# into an unbounded per-hour query loop.
FAILING_STREAK_LOOKBACK_HOURS = 24 * 14

# A custom start/end range charts hourly up to this span, daily beyond it.
CUSTOM_HOURLY_SPAN = datetime.timedelta(days=3)

# Extent/calendar series are capped at roughly this many buckets.
MAX_EXTENT_BUCKETS = 120

TOP_N_BREAKDOWN_SERIES = 7

RATE_LINE_ITEMS: list[tuple[str, str, str, str, str | None]] = [
    ("vcpu", "vCPU", "vcpu_hour", "USD/vCPU-hr", None),
    ("memory", "Memory", "memory_gib_hour", "USD/GiB-hr", None),
    ("storage", "Storage (gp3)", "storage_gib_month", "USD/GiB-mo", None),
    ("egress", "Egress", "egress_gb", "USD/GB", None),
]
FIXED_RATE_ITEMS: list[tuple[str, str]] = [
    ("eks_control_plane", "EKS control plane"),
    ("nat_gateway", "NAT gateway"),
    ("load_balancer", "Load balancer"),
]
RATES_SOURCE = {
    "region": "us-east-2",
    "checked": "2026-10-11",
    "basis": "AWS on-demand list price",
}

WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# Postgres EXTRACT(dow ...) is 0=Sunday..6=Saturday; map Mon..Sun display
# order to dow values.
WEEKDAY_DOW = [1, 2, 3, 4, 5, 6, 0]

PRIMARY_BREAKDOWN = {
    "platform": "organizations",
    "organization": "projects",
    "project": "applications",
    "environment": "applications",
    "application": "processes",
}


@dataclass
class CostScope:
    kind: str
    # Every kind except "platform" (admin-only, whole-install view) needs an
    # organization; platform aggregates across all of them.
    organization: Organization | None = None
    project: Project | None = None
    environment: Environment | None = None
    application: Application | None = None

    @property
    def name(self) -> str:
        if self.kind == "platform":
            return "Platform"
        if self.kind == "application" and self.application is not None:
            return self.application.name
        if self.kind == "environment" and self.environment is not None:
            return self.environment.name
        if self.kind == "project" and self.project is not None:
            return self.project.name
        if self.organization is None:
            raise ValueError("Non-platform cost scopes require an organization")
        return self.organization.name


def build_report(
    scope: CostScope,
    range_key: str,
    now: datetime.datetime | None = None,
    start: datetime.datetime | str | None = None,
    end: datetime.datetime | str | None = None,
) -> dict[str, object]:
    now = _as_utc(now or datetime.datetime.now(datetime.UTC))
    rates = effective_rates()

    first_hour, last_hour = _extent()

    if start is not None or end is not None:
        range_start, range_end, interval, label = _resolve_custom_range(
            start, end, now, first_hour, last_hour
        )
    else:
        range_start, range_end, interval, label = _resolve_range(range_key, now)
    duration = range_end - range_start
    previous_start = range_start - duration
    previous_end = range_start

    totals = _category_totals(scope, range_start, range_end)
    previous_totals = _category_totals(scope, previous_start, previous_end)
    cost = sum(totals.values())
    previous_cost = sum(previous_totals.values())
    change_pct = (
        ((cost - previous_cost) / previous_cost * 100.0) if previous_cost else None
    )

    meters = _meter_totals(scope, range_start, range_end)

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    mtd_cost = (
        cost
        if range_key == "mtd"
        else sum(_category_totals(scope, month_start, now).values())
    )

    if now.month == 12:
        next_month_start = now.replace(
            year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    else:
        next_month_start = now.replace(
            month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    hours_in_month = (next_month_start - month_start).total_seconds() / 3600.0
    elapsed_hours = (now - month_start).total_seconds() / 3600.0
    remaining_hours = max(hours_in_month - elapsed_hours, 0.0)

    run_rate_hourly = _run_rate_hourly(scope, now)
    projected_month = (
        mtd_cost + run_rate_hourly * remaining_hours
        if run_rate_hourly is not None
        else None
    )
    run_rate_monthly = (
        run_rate_hourly * hours_in_month if run_rate_hourly is not None else None
    )
    efficiency = _efficiency(scope, range_start, range_end, rates)

    buckets = _bucket_starts(range_start, range_end, interval)
    series = _series(scope, range_start, range_end, interval, buckets)
    series["previous"] = _series_total(
        scope,
        previous_start,
        previous_end,
        interval,
        _bucket_starts(previous_start, previous_end, interval),
    )
    series["efficiency"] = _series_efficiency(
        scope, range_start, range_end, interval, buckets, rates
    )

    breakdowns: dict[str, list[BreakdownRow]] = {}
    raw_keys_by_name: dict[str, list[BreakdownKey]] = {}
    breakdown_funcs: dict[str, BreakdownFunc] = _BREAKDOWN_FUNCS[scope.kind]
    for name, breakdown_func in breakdown_funcs.items():
        rows, raw_keys = breakdown_func(
            scope, range_start, range_end, previous_start, previous_end, rates
        )
        breakdowns[name] = rows
        raw_keys_by_name[name] = raw_keys

    series["breakdowns"] = _series_breakdowns(
        scope, range_start, range_end, interval, buckets, raw_keys_by_name
    )

    for name, rows in breakdowns.items():
        column, category_filter = _BREAKDOWN_DIMENSIONS[name]
        raw_keys = raw_keys_by_name[name]
        top_keys = raw_keys[:TOP_N_BREAKDOWN_SERIES]
        meters_by_key, resource_series_by_key = _breakdown_detail(
            scope,
            column,
            category_filter,
            top_keys,
            range_start,
            range_end,
            interval,
            buckets,
            rates,
        )
        top_key_set = set(top_keys)
        for row, raw_key in zip(rows, raw_keys, strict=True):
            if raw_key in top_key_set:
                row["meters"] = meters_by_key.get(raw_key, [])
                row["resource_series"] = resource_series_by_key.get(raw_key)
            else:
                row["meters"] = None
                row["resource_series"] = None

    primary_name = PRIMARY_BREAKDOWN[scope.kind]
    movers = _movers(breakdowns[primary_name])

    heatmap = _heatmap(scope, range_start, range_end)
    freshness = _freshness(range_start, range_end, now)

    extent_series = _extent_series(scope, first_hour, last_hour)
    rates_display = _rates_display(rates)

    platform: PlatformBlock | None = None
    if scope.kind == "platform":
        empty_breakdown: SeriesBreakdownEntry = {
            "keys": [],
            "names": {},
            "values": {},
            "other": [0.0] * len(buckets),
        }
        platform = {
            "reconciliation": _platform_reconciliation(range_start, range_end),
            "capacity_series": _platform_capacity_series(
                scope, range_start, range_end, interval, buckets, rates
            ),
            "nodes": _platform_nodes(rates, range_start, range_end),
            "unallocated": _platform_unallocated(range_start, range_end),
            "top_orgs_series": series["breakdowns"].get(
                "organizations", empty_breakdown
            ),
        }

    return {
        "scope": {"kind": scope.kind, "name": scope.name},
        "range": {
            "key": range_key,
            "label": label,
            "start": range_start.isoformat(),
            "end": range_end.isoformat(),
            "interval": interval,
        },
        "totals": {
            "cost": cost,
            "previous_cost": previous_cost,
            "change_pct": change_pct,
        },
        "kpis": {
            "month_to_date": mtd_cost,
            "projected_month": projected_month,
            "run_rate_monthly": run_rate_monthly,
            "efficiency": efficiency,
        },
        "categories": [
            {
                "key": category,
                "label": CATEGORY_LABELS[category],
                "cost": totals.get(category, 0.0),
                "share": (totals.get(category, 0.0) / cost) if cost else 0.0,
            }
            for category in CATEGORY_ORDER
        ],
        "meters": [
            {
                "key": meter,
                "label": METER_LABELS[meter],
                "unit": METER_UNITS[meter],
                "quantity": meters.get(meter, (0.0, 0.0))[0],
                "rate": _rate_for_meter_optional(rates, meter),
                "cost": meters.get(meter, (0.0, 0.0))[1],
            }
            for meter in METER_ORDER
        ],
        "series": series,
        "breakdowns": breakdowns,
        "freshness": freshness,
        "rates": rates_display,
        "rates_raw": rates,
        "rates_source": RATES_SOURCE,
        "extent": {
            "first_hour": first_hour.isoformat() if first_hour is not None else None,
            "last_hour": last_hour.isoformat() if last_hour is not None else None,
        },
        "extent_series": extent_series,
        "calendar": extent_series,
        "heatmap": heatmap,
        "movers": movers,
        "platform": platform,
    }


# ---------------------------------------------------------------------------
# Time ranges
# ---------------------------------------------------------------------------


def _as_utc(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


def _parse_range_param(value: datetime.datetime | str) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return _as_utc(value)
    parsed = datetime.datetime.fromisoformat(value)
    return _as_utc(parsed)


def _resolve_range(
    range_key: str, now: datetime.datetime
) -> tuple[datetime.datetime, datetime.datetime, Interval, str]:
    if range_key == "mtd":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now
        interval: Interval = "day"
        label = "Month to date"
    else:
        config = RANGE_CONFIG.get(range_key)
        if config is None:
            raise ValueError(f"unknown cost report range: {range_key!r}")
        end = now
        start = end - config["duration"]
        interval = config["interval"]
        label = config["label"]
    return start, end, interval, label


def _resolve_custom_range(
    start: datetime.datetime | str | None,
    end: datetime.datetime | str | None,
    now: datetime.datetime,
    first_hour: datetime.datetime | None,
    last_hour: datetime.datetime | None,
) -> tuple[datetime.datetime, datetime.datetime, Interval, str]:
    range_start = (
        _parse_range_param(start) if start is not None else (first_hour or now)
    )
    range_end = _parse_range_param(end) if end is not None else now

    # Clamp the low end to the start of available data; clamp the high end
    # only to "now" (not to last_hour), so a requested range can still reach
    # into the most recent, possibly-not-yet-collected, in-progress hour.
    if first_hour is not None and range_start < first_hour:
        range_start = first_hour
    range_end = min(range_end, now)
    if range_end <= range_start:
        range_end = range_start + datetime.timedelta(hours=1)

    span = range_end - range_start
    interval: Interval = "hour" if span <= CUSTOM_HOURLY_SPAN else "day"
    label = f"{range_start.date().isoformat()} – {range_end.date().isoformat()}"
    return range_start, range_end, interval, label


def _bucket_starts(
    start: datetime.datetime, end: datetime.datetime, interval: Interval
) -> list[datetime.datetime]:
    # Buckets must start at the first boundary >= start (ceiling, not floor):
    # the series query below filters CostLineItem.period_start >= start, so a
    # floored boundary before `start` would never match any row and would
    # show up as a spurious always-zero leading bucket.
    if interval == "hour":
        cursor = start.replace(minute=0, second=0, microsecond=0)
        if cursor < start:
            cursor += datetime.timedelta(hours=1)
        step = datetime.timedelta(hours=1)
    elif interval == "week":
        cursor = (start - datetime.timedelta(days=start.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if cursor < start:
            cursor += datetime.timedelta(weeks=1)
        step = datetime.timedelta(weeks=1)
    else:
        cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
        if cursor < start:
            cursor += datetime.timedelta(days=1)
        step = datetime.timedelta(days=1)
    buckets: list[datetime.datetime] = []
    while cursor < end:
        buckets.append(cursor)
        cursor += step
    return buckets


# ---------------------------------------------------------------------------
# Scope filtering
# ---------------------------------------------------------------------------


def _scope_filter[Q](query: Q, scope: CostScope) -> Q:
    q = cast("Query[object]", query)
    if scope.kind == "platform":
        return cast(Q, q)
    if scope.organization is None:
        raise ValueError("Non-platform cost scopes require an organization")
    q = q.filter(CostLineItem.organization_id == scope.organization.id)
    if scope.kind == "project" and scope.project is not None:
        q = q.filter(CostLineItem.project_id == scope.project.id)
    elif scope.kind == "environment" and scope.environment is not None:
        q = q.filter(CostLineItem.environment_id == scope.environment.id)
    elif scope.kind == "application" and scope.application is not None:
        q = q.filter(CostLineItem.application_id == scope.application.id)
        if scope.environment is not None:
            q = q.filter(CostLineItem.environment_id == scope.environment.id)
    return cast(Q, q)


# ---------------------------------------------------------------------------
# Totals / categories / meters / series
# ---------------------------------------------------------------------------


def _category_totals(
    scope: CostScope, start: datetime.datetime, end: datetime.datetime
) -> dict[str, float]:
    query = _scope_filter(
        db.session.query(
            CostLineItem.category, func.sum(CostLineItem.cost_usd).label("cost")
        ),
        scope,
    ).filter(CostLineItem.period_start >= start, CostLineItem.period_start < end)
    rows = query.group_by(CostLineItem.category).all()
    return {row.category: float(row.cost) for row in rows}


def _meter_totals(
    scope: CostScope, start: datetime.datetime, end: datetime.datetime
) -> dict[str, tuple[float, float]]:
    query = _scope_filter(
        db.session.query(
            CostLineItem.meter,
            func.sum(CostLineItem.quantity).label("quantity"),
            func.sum(CostLineItem.cost_usd).label("cost"),
        ),
        scope,
    ).filter(CostLineItem.period_start >= start, CostLineItem.period_start < end)
    rows = query.group_by(CostLineItem.meter).all()
    return {row.meter: (float(row.quantity), float(row.cost)) for row in rows}


def _series(
    scope: CostScope,
    start: datetime.datetime,
    end: datetime.datetime,
    interval: Interval,
    buckets: list[datetime.datetime],
) -> SeriesPayload:
    trunc = func.date_trunc(interval, CostLineItem.period_start)
    query = _scope_filter(
        db.session.query(
            trunc.label("bucket"),
            CostLineItem.category,
            func.sum(CostLineItem.cost_usd).label("cost"),
        ),
        scope,
    ).filter(CostLineItem.period_start >= start, CostLineItem.period_start < end)
    rows = query.group_by("bucket", CostLineItem.category).all()

    by_bucket: dict[datetime.datetime, dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    for row in rows:
        bucket_dt = _as_utc(row.bucket)
        by_bucket[bucket_dt][row.category] += float(row.cost)

    timestamps = [int(bucket.timestamp()) for bucket in buckets]
    categories = {
        category: [by_bucket.get(bucket, {}).get(category, 0.0) for bucket in buckets]
        for category in CATEGORY_ORDER
    }
    return {"timestamps": timestamps, "categories": categories}


def _series_total(
    scope: CostScope,
    start: datetime.datetime,
    end: datetime.datetime,
    interval: Interval,
    buckets: list[datetime.datetime],
) -> list[float]:
    trunc = func.date_trunc(interval, CostLineItem.period_start)
    query = _scope_filter(
        db.session.query(
            trunc.label("bucket"), func.sum(CostLineItem.cost_usd).label("cost")
        ),
        scope,
    ).filter(CostLineItem.period_start >= start, CostLineItem.period_start < end)
    rows = query.group_by("bucket").all()
    by_bucket = {_as_utc(row.bucket): float(row.cost) for row in rows}
    return [by_bucket.get(bucket, 0.0) for bucket in buckets]


def _series_efficiency(
    scope: CostScope,
    start: datetime.datetime,
    end: datetime.datetime,
    interval: Interval,
    buckets: list[datetime.datetime],
    rates: CostRates,
) -> EfficiencySeries:
    trunc = func.date_trunc(interval, CostLineItem.period_start)
    query = _scope_filter(
        db.session.query(
            trunc.label("bucket"),
            CostLineItem.meter,
            func.sum(CostLineItem.usage_quantity).label("usage"),
            func.sum(CostLineItem.request_quantity).label("request"),
            func.sum(CostLineItem.quantity).label("billed"),
        ),
        scope,
    ).filter(
        CostLineItem.period_start >= start,
        CostLineItem.period_start < end,
        CostLineItem.meter.in_(["vcpu", "memory"]),
        CostLineItem.category.in_(["compute", "backing_service"]),
    )
    usage_by_bucket: dict[datetime.datetime, float] = defaultdict(float)
    request_by_bucket: dict[datetime.datetime, float] = defaultdict(float)
    billed_by_bucket: dict[datetime.datetime, float] = defaultdict(float)
    for row in query.group_by("bucket", CostLineItem.meter).all():
        bucket = _as_utc(row.bucket)
        rate = _rate_for_meter(rates, row.meter)
        usage_by_bucket[bucket] += float(row.usage or 0.0) * rate
        request_by_bucket[bucket] += float(row.request or 0.0) * rate
        billed_by_bucket[bucket] += float(row.billed or 0.0) * rate
    return {
        "usage": [usage_by_bucket.get(bucket, 0.0) for bucket in buckets],
        "request": [request_by_bucket.get(bucket, 0.0) for bucket in buckets],
        "billed": [billed_by_bucket.get(bucket, 0.0) for bucket in buckets],
    }


def _series_breakdowns(
    scope: CostScope,
    start: datetime.datetime,
    end: datetime.datetime,
    interval: Interval,
    buckets: list[datetime.datetime],
    raw_keys_by_name: dict[str, list[BreakdownKey]],
) -> dict[str, SeriesBreakdownEntry]:
    trunc = func.date_trunc(interval, CostLineItem.period_start)
    result: dict[str, SeriesBreakdownEntry] = {}
    for name, raw_keys_all in raw_keys_by_name.items():
        top_keys = raw_keys_all[:TOP_N_BREAKDOWN_SERIES]
        column, category_filter = _BREAKDOWN_DIMENSIONS[name]
        if not top_keys:
            result[name] = {
                "keys": [],
                "names": {},
                "values": {},
                "other": [0.0] * len(buckets),
            }
            continue

        query = _scope_filter(
            db.session.query(
                trunc.label("bucket"),
                column.label("key"),
                func.sum(CostLineItem.cost_usd).label("cost"),
            ),
            scope,
        ).filter(
            CostLineItem.period_start >= start,
            CostLineItem.period_start < end,
            column.isnot(None),
        )
        if category_filter:
            query = query.filter(CostLineItem.category.in_(category_filter))

        top_key_set = set(top_keys)
        values_by_key: dict[BreakdownKey, dict[datetime.datetime, float]] = {
            key: defaultdict(float) for key in top_keys
        }
        other_by_bucket: dict[datetime.datetime, float] = defaultdict(float)
        for row in query.group_by("bucket", "key").all():
            bucket = _as_utc(row.bucket)
            if row.key in top_key_set:
                values_by_key[row.key][bucket] += float(row.cost)
            else:
                other_by_bucket[bucket] += float(row.cost)

        names = _resolve_names(name, top_keys)
        result[name] = {
            "keys": [str(key) for key in top_keys],
            "names": {str(key): names.get(key, str(key)) for key in top_keys},
            "values": {
                str(key): [values_by_key[key].get(bucket, 0.0) for bucket in buckets]
                for key in top_keys
            },
            "other": [other_by_bucket.get(bucket, 0.0) for bucket in buckets],
        }
    return result


def _resolve_names(
    breakdown_name: str, keys: list[BreakdownKey]
) -> dict[BreakdownKey, str]:
    names: dict[BreakdownKey, str] = {}
    if breakdown_name == "processes":
        return {key: str(key) for key in keys}
    if breakdown_name == "organizations":
        for key in keys:
            organization: Organization | None = Organization.query.get(key)
            names[key] = organization.name if organization is not None else "(deleted)"
    elif breakdown_name == "projects":
        for key in keys:
            project: Project | None = Project.query.get(key)
            names[key] = project.name if project is not None else "(deleted)"
    elif breakdown_name == "environments":
        for key in keys:
            environment: Environment | None = Environment.query.get(key)
            names[key] = environment.name if environment is not None else "(deleted)"
    elif breakdown_name == "applications":
        for key in keys:
            application: Application | None = Application.query.get(key)
            names[key] = application.name if application is not None else "(deleted)"
    elif breakdown_name == "backing_services":
        for key in keys:
            resource: Resource | None = Resource.query.get(key)
            names[key] = resource.name if resource is not None else "(deleted)"
    return names


def _breakdown_detail(
    scope: CostScope,
    column: InstrumentedAttribute[BreakdownKey | None],
    category_filter: list[str] | None,
    keys: list[BreakdownKey],
    start: datetime.datetime,
    end: datetime.datetime,
    interval: Interval,
    buckets: list[datetime.datetime],
    rates: CostRates,
) -> tuple[
    dict[BreakdownKey, list[MeterRow]], dict[BreakdownKey, dict[str, list[float]]]
]:
    if not keys:
        return {}, {}

    totals_query = _scope_filter(
        db.session.query(
            column.label("key"),
            CostLineItem.meter,
            func.sum(CostLineItem.quantity).label("quantity"),
            func.sum(CostLineItem.cost_usd).label("cost"),
        ),
        scope,
    ).filter(
        CostLineItem.period_start >= start,
        CostLineItem.period_start < end,
        column.in_(keys),
    )
    if category_filter:
        totals_query = totals_query.filter(CostLineItem.category.in_(category_filter))
    totals_by_key: dict[BreakdownKey, dict[str, tuple[float, float]]] = defaultdict(
        dict
    )
    for row in totals_query.group_by("key", CostLineItem.meter).all():
        totals_by_key[row.key][row.meter] = (float(row.quantity), float(row.cost))

    meters_by_key: dict[BreakdownKey, list[MeterRow]] = {}
    for key in keys:
        meters_by_key[key] = [
            {
                "key": meter,
                "label": METER_LABELS[meter],
                "unit": METER_UNITS[meter],
                "quantity": totals_by_key[key].get(meter, (0.0, 0.0))[0],
                "rate": _rate_for_meter_optional(rates, meter),
                "cost": totals_by_key[key].get(meter, (0.0, 0.0))[1],
            }
            for meter in METER_ORDER
        ]

    trunc = func.date_trunc(interval, CostLineItem.period_start)
    series_query = _scope_filter(
        db.session.query(
            trunc.label("bucket"),
            column.label("key"),
            CostLineItem.meter,
            func.sum(CostLineItem.quantity).label("quantity"),
        ),
        scope,
    ).filter(
        CostLineItem.period_start >= start,
        CostLineItem.period_start < end,
        column.in_(keys),
    )
    if category_filter:
        series_query = series_query.filter(CostLineItem.category.in_(category_filter))
    series_tmp: dict[BreakdownKey, dict[str, dict[datetime.datetime, float]]] = (
        defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    )
    for row in series_query.group_by("bucket", "key", CostLineItem.meter).all():
        series_tmp[row.key][row.meter][_as_utc(row.bucket)] += float(row.quantity)

    resource_series_by_key: dict[BreakdownKey, dict[str, list[float]]] = {}
    for key in keys:
        resource_series_by_key[key] = {
            meter: [series_tmp[key][meter].get(bucket, 0.0) for bucket in buckets]
            for meter in ("vcpu", "memory", "egress", "storage")
        }

    return meters_by_key, resource_series_by_key


def _run_rate_hourly(scope: CostScope, now: datetime.datetime) -> float | None:
    cutoff = now.replace(minute=0, second=0, microsecond=0)
    recent_runs: list[CostCollectionRun] = (
        CostCollectionRun.query.filter(
            CostCollectionRun.status == "succeeded",
            CostCollectionRun.period_start < cutoff,
        )
        .order_by(CostCollectionRun.period_start.desc())
        .limit(24)
        .all()
    )
    if not recent_runs:
        return None
    hours = [run.period_start for run in recent_runs]
    total: float | None = (
        _scope_filter(
            db.session.query(func.coalesce(func.sum(CostLineItem.cost_usd), 0)), scope
        )
        .filter(CostLineItem.period_start.in_(hours))
        .scalar()
    )
    return float(total or 0.0) / len(hours)


def _efficiency(
    scope: CostScope, start: datetime.datetime, end: datetime.datetime, rates: CostRates
) -> float | None:
    query = _scope_filter(
        db.session.query(
            CostLineItem.meter,
            func.sum(CostLineItem.usage_quantity).label("usage"),
            func.sum(CostLineItem.request_quantity).label("request"),
        ),
        scope,
    ).filter(
        CostLineItem.period_start >= start,
        CostLineItem.period_start < end,
        CostLineItem.meter.in_(["vcpu", "memory"]),
        CostLineItem.category.in_(["compute", "backing_service"]),
    )
    rows = query.group_by(CostLineItem.meter).all()
    usage_cost = 0.0
    request_cost = 0.0
    for row in rows:
        rate = _rate_for_meter(rates, row.meter)
        usage_cost += float(row.usage or 0.0) * rate
        request_cost += float(row.request or 0.0) * rate
    if request_cost <= 0:
        return None
    return usage_cost / request_cost


# ---------------------------------------------------------------------------
# Breakdowns
# ---------------------------------------------------------------------------

_BREAKDOWN_DIMENSIONS: dict[str, BreakdownDimension] = {
    "organizations": (CostLineItem.organization_id, None),
    "projects": (CostLineItem.project_id, None),
    "environments": (CostLineItem.environment_id, None),
    "applications": (CostLineItem.application_id, None),
    "backing_services": (CostLineItem.resource_id, ["backing_service", "storage"]),
    "processes": (CostLineItem.component, ["compute", "egress"]),
}


def _aggregate_breakdown(
    scope: CostScope,
    column: InstrumentedAttribute[BreakdownKey | None],
    start: datetime.datetime,
    end: datetime.datetime,
    previous_start: datetime.datetime,
    previous_end: datetime.datetime,
    rates: CostRates,
    category_filter: list[str] | None = None,
) -> BreakdownAggregates:
    cat_query = _scope_filter(
        db.session.query(
            column.label("key"),
            CostLineItem.category,
            func.sum(CostLineItem.cost_usd).label("cost"),
        ),
        scope,
    ).filter(
        CostLineItem.period_start >= start,
        CostLineItem.period_start < end,
        column.isnot(None),
    )
    if category_filter:
        cat_query = cat_query.filter(CostLineItem.category.in_(category_filter))
    cat_rows = cat_query.group_by("key", CostLineItem.category).all()

    categories_by_key: dict[BreakdownKey, dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    totals_by_key: dict[BreakdownKey, float] = defaultdict(float)
    for row in cat_rows:
        categories_by_key[row.key][row.category] += float(row.cost)
        totals_by_key[row.key] += float(row.cost)

    prev_query = _scope_filter(
        db.session.query(
            column.label("key"), func.sum(CostLineItem.cost_usd).label("cost")
        ),
        scope,
    ).filter(
        CostLineItem.period_start >= previous_start,
        CostLineItem.period_start < previous_end,
        column.isnot(None),
    )
    if category_filter:
        prev_query = prev_query.filter(CostLineItem.category.in_(category_filter))
    previous_by_key = {
        row.key: float(row.cost) for row in prev_query.group_by("key").all()
    }

    eff_query = _scope_filter(
        db.session.query(
            column.label("key"),
            CostLineItem.meter,
            func.sum(CostLineItem.usage_quantity).label("usage"),
            func.sum(CostLineItem.request_quantity).label("request"),
        ),
        scope,
    ).filter(
        CostLineItem.period_start >= start,
        CostLineItem.period_start < end,
        column.isnot(None),
        CostLineItem.meter.in_(["vcpu", "memory"]),
        CostLineItem.category.in_(["compute", "backing_service"]),
    )
    efficiency_num: dict[BreakdownKey, float] = defaultdict(float)
    efficiency_den: dict[BreakdownKey, float] = defaultdict(float)
    for row in eff_query.group_by("key", CostLineItem.meter).all():
        rate = _rate_for_meter(rates, row.meter)
        efficiency_num[row.key] += float(row.usage or 0.0) * rate
        efficiency_den[row.key] += float(row.request or 0.0) * rate

    return (
        totals_by_key,
        categories_by_key,
        previous_by_key,
        efficiency_num,
        efficiency_den,
    )


def _build_rows(
    totals_by_key: dict[BreakdownKey, float],
    categories_by_key: dict[BreakdownKey, dict[str, float]],
    previous_by_key: dict[BreakdownKey, float],
    efficiency_num: dict[BreakdownKey, float],
    efficiency_den: dict[BreakdownKey, float],
    name_resolver: NameResolver,
) -> tuple[list[BreakdownRow], list[BreakdownKey]]:
    total_cost = sum(totals_by_key.values())
    rows: list[BreakdownRow] = []
    row_costs: list[float] = []
    raw_keys_tmp: list[BreakdownKey] = []
    for key, cost in totals_by_key.items():
        resolved = name_resolver(key)
        if resolved is None:
            continue
        name, context, slugs, deleted = resolved
        efficiency = (
            efficiency_num[key] / efficiency_den[key]
            if efficiency_den.get(key)
            else None
        )
        rows.append(
            {
                "key": str(key),
                "name": name,
                "context": context,
                "cost": cost,
                "previous_cost": previous_by_key.get(key, 0.0),
                "share": (cost / total_cost) if total_cost else 0.0,
                "categories": {
                    category: categories_by_key[key].get(category, 0.0)
                    for category in CATEGORY_ORDER
                },
                "efficiency": efficiency,
                "slugs": slugs,
                "deleted": deleted,
                "meters": None,
                "resource_series": None,
            }
        )
        row_costs.append(float(cost))
        raw_keys_tmp.append(key)
    order = sorted(range(len(rows)), key=lambda i: row_costs[i], reverse=True)
    rows = [rows[i] for i in order]
    raw_keys = [raw_keys_tmp[i] for i in order]
    return rows, raw_keys


def _breakdown_organizations(
    scope: CostScope,
    start: datetime.datetime,
    end: datetime.datetime,
    previous_start: datetime.datetime,
    previous_end: datetime.datetime,
    rates: CostRates,
) -> tuple[list[BreakdownRow], list[BreakdownKey]]:
    aggregates = _aggregate_breakdown(
        scope,
        CostLineItem.organization_id,
        start,
        end,
        previous_start,
        previous_end,
        rates,
    )

    def resolver(key: BreakdownKey) -> ResolvedName:
        organization: Organization | None = Organization.query.get(key)
        if organization is None:
            return (
                "(deleted organization)",
                None,
                {
                    "organization": None,
                    "project": None,
                    "environment": None,
                    "application": None,
                },
                True,
            )
        return (
            organization.name,
            None,
            {
                "organization": organization.slug,
                "project": None,
                "environment": None,
                "application": None,
            },
            organization.deleted_at is not None,
        )

    return _build_rows(*aggregates, name_resolver=resolver)


def _breakdown_projects(
    scope: CostScope,
    start: datetime.datetime,
    end: datetime.datetime,
    previous_start: datetime.datetime,
    previous_end: datetime.datetime,
    rates: CostRates,
) -> tuple[list[BreakdownRow], list[BreakdownKey]]:
    aggregates = _aggregate_breakdown(
        scope, CostLineItem.project_id, start, end, previous_start, previous_end, rates
    )

    def resolver(key: BreakdownKey) -> ResolvedName:
        project: Project | None = Project.query.get(key)
        if project is None:
            return (
                "(deleted project)",
                None,
                {"project": None, "environment": None, "application": None},
                True,
            )
        # Platform scope spans every organization, so the context line and
        # link need to identify which org this project belongs to.
        context = project.organization.name if scope.kind == "platform" else None
        slugs: BreakdownSlugs = {
            "project": project.slug,
            "environment": None,
            "application": None,
        }
        if scope.kind == "platform":
            slugs["organization"] = project.organization.slug
        return (project.name, context, slugs, project.deleted_at is not None)

    return _build_rows(*aggregates, name_resolver=resolver)


def _breakdown_environments(
    scope: CostScope,
    start: datetime.datetime,
    end: datetime.datetime,
    previous_start: datetime.datetime,
    previous_end: datetime.datetime,
    rates: CostRates,
) -> tuple[list[BreakdownRow], list[BreakdownKey]]:
    aggregates = _aggregate_breakdown(
        scope,
        CostLineItem.environment_id,
        start,
        end,
        previous_start,
        previous_end,
        rates,
    )

    def resolver(key: BreakdownKey) -> ResolvedName:
        environment: Environment | None = Environment.query.get(key)
        if environment is None:
            return (
                "(deleted environment)",
                None,
                {"project": None, "environment": None, "application": None},
                True,
            )
        project = environment.project
        return (
            environment.name,
            project.name if project is not None else None,
            {
                "project": project.slug if project is not None else None,
                "environment": environment.slug,
                "application": None,
            },
            environment.deleted_at is not None,
        )

    return _build_rows(*aggregates, name_resolver=resolver)


def _breakdown_applications(
    scope: CostScope,
    start: datetime.datetime,
    end: datetime.datetime,
    previous_start: datetime.datetime,
    previous_end: datetime.datetime,
    rates: CostRates,
) -> tuple[list[BreakdownRow], list[BreakdownKey]]:
    aggregates = _aggregate_breakdown(
        scope,
        CostLineItem.application_id,
        start,
        end,
        previous_start,
        previous_end,
        rates,
    )

    def resolver(key: BreakdownKey) -> ResolvedName:
        application: Application | None = Application.query.get(key)
        if application is None:
            return (
                "(deleted application)",
                None,
                {"project": None, "environment": None, "application": None},
                True,
            )
        project = application.project
        if scope.kind == "platform" and project is not None:
            context = f"{project.organization.name} / {project.name}"
        else:
            context = project.name if project is not None else None
        slugs: BreakdownSlugs = {
            "project": project.slug if project is not None else None,
            "environment": None,
            "application": application.slug,
        }
        if scope.kind == "platform" and project is not None:
            slugs["organization"] = project.organization.slug
        return (application.name, context, slugs, application.deleted_at is not None)

    return _build_rows(*aggregates, name_resolver=resolver)


def _breakdown_backing_services(
    scope: CostScope,
    start: datetime.datetime,
    end: datetime.datetime,
    previous_start: datetime.datetime,
    previous_end: datetime.datetime,
    rates: CostRates,
) -> tuple[list[BreakdownRow], list[BreakdownKey]]:
    aggregates = _aggregate_breakdown(
        scope,
        CostLineItem.resource_id,
        start,
        end,
        previous_start,
        previous_end,
        rates,
        category_filter=["backing_service", "storage"],
    )

    def resolver(key: BreakdownKey) -> ResolvedName:
        resource: Resource | None = Resource.query.get(key)
        if resource is None:
            return (
                "(deleted resource)",
                None,
                {"project": None, "environment": None, "application": None},
                True,
            )
        environment = resource.environment
        project = environment.project if environment is not None else None
        return (
            resource.name,
            resource.type,
            {
                "project": project.slug if project is not None else None,
                "environment": environment.slug if environment is not None else None,
                "application": None,
            },
            resource.deleted_at is not None,
        )

    return _build_rows(*aggregates, name_resolver=resolver)


def _breakdown_processes(
    scope: CostScope,
    start: datetime.datetime,
    end: datetime.datetime,
    previous_start: datetime.datetime,
    previous_end: datetime.datetime,
    rates: CostRates,
) -> tuple[list[BreakdownRow], list[BreakdownKey]]:
    aggregates = _aggregate_breakdown(
        scope,
        CostLineItem.component,
        start,
        end,
        previous_start,
        previous_end,
        rates,
        category_filter=["compute", "egress"],
    )

    application_slug = scope.application.slug if scope.application is not None else None
    project_slug = (
        scope.application.project.slug
        if scope.application is not None and scope.application.project is not None
        else None
    )

    def resolver(key: BreakdownKey) -> ResolvedName:
        return (
            str(key),
            None,
            {
                "project": project_slug,
                "environment": None,
                "application": application_slug,
            },
            False,
        )

    return _build_rows(*aggregates, name_resolver=resolver)


_BREAKDOWN_FUNCS: dict[str, dict[str, BreakdownFunc]] = {
    "platform": {
        "organizations": _breakdown_organizations,
        "projects": _breakdown_projects,
        "applications": _breakdown_applications,
    },
    "organization": {
        "projects": _breakdown_projects,
        "applications": _breakdown_applications,
    },
    "project": {
        "environments": _breakdown_environments,
        "applications": _breakdown_applications,
    },
    "environment": {
        "applications": _breakdown_applications,
        "backing_services": _breakdown_backing_services,
    },
    "application": {
        "environments": _breakdown_environments,
        "processes": _breakdown_processes,
    },
}


def _movers(rows: list[BreakdownRow], limit: int = 5) -> list[MoverRow]:
    ranked = sorted(
        rows, key=lambda row: abs(row["cost"] - row["previous_cost"]), reverse=True
    )
    movers: list[MoverRow] = []
    for row in ranked[:limit]:
        change = row["cost"] - row["previous_cost"]
        change_pct = (
            (change / row["previous_cost"] * 100.0) if row["previous_cost"] else None
        )
        movers.append(
            {
                "key": row["key"],
                "name": row["name"],
                "context": row["context"],
                "cost": row["cost"],
                "previous_cost": row["previous_cost"],
                "change": change,
                "change_pct": change_pct,
                "slugs": row["slugs"],
            }
        )
    return movers


# ---------------------------------------------------------------------------
# Extent / calendar
# ---------------------------------------------------------------------------


def _extent() -> tuple[datetime.datetime | None, datetime.datetime | None]:
    first: CostCollectionRun | None = (
        CostCollectionRun.query.filter_by(status="succeeded")
        .order_by(CostCollectionRun.period_start.asc())
        .first()
    )
    last: CostCollectionRun | None = (
        CostCollectionRun.query.filter_by(status="succeeded")
        .order_by(CostCollectionRun.period_start.desc())
        .first()
    )
    if first is None or last is None:
        return None, None
    return first.period_start, last.period_start + datetime.timedelta(hours=1)


def _extent_series(
    scope: CostScope,
    first_hour: datetime.datetime | None,
    last_hour: datetime.datetime | None,
) -> SeriesPayload:
    if first_hour is None or last_hour is None:
        return {
            "timestamps": [],
            "categories": {category: [] for category in CATEGORY_ORDER},
        }
    span_days = (last_hour - first_hour).total_seconds() / 86400.0
    interval: Interval = "day" if span_days <= MAX_EXTENT_BUCKETS else "week"
    buckets = _bucket_starts(first_hour, last_hour, interval)
    return _series(scope, first_hour, last_hour, interval, buckets)


# ---------------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------------


class HeatmapPayload(TypedDict):
    days: list[str]
    hours: list[int]
    values: list[list[float]]


def _heatmap(
    scope: CostScope, start: datetime.datetime, end: datetime.datetime
) -> HeatmapPayload:
    hour_totals = (
        _scope_filter(
            db.session.query(
                CostLineItem.period_start.label("period_start"),
                func.sum(CostLineItem.cost_usd).label("cost"),
            ),
            scope,
        )
        .filter(CostLineItem.period_start >= start, CostLineItem.period_start < end)
        .group_by(CostLineItem.period_start)
        .subquery()
    )
    rows = (
        db.session.query(
            func.extract("dow", hour_totals.c.period_start).label("dow"),
            func.extract("hour", hour_totals.c.period_start).label("hour"),
            func.avg(hour_totals.c.cost).label("avg_cost"),
        )
        .group_by("dow", "hour")
        .all()
    )
    by_dow_hour = {(int(row.dow), int(row.hour)): float(row.avg_cost) for row in rows}
    values = [
        [by_dow_hour.get((dow, hour), 0.0) for hour in range(24)] for dow in WEEKDAY_DOW
    ]
    return {"days": WEEKDAY_LABELS, "hours": list(range(24)), "values": values}


# ---------------------------------------------------------------------------
# Platform (admin-only, whole-install) block
# ---------------------------------------------------------------------------


def _platform_reconciliation(
    start: datetime.datetime, end: datetime.datetime
) -> ReconciliationStats:
    """Admins' view of "does tenant billing add up to what AWS actually
    charges for compute capacity": cluster_compute + fixed_overhead is the
    ground truth (node-hours at list price, plus always-on shared
    infrastructure). `tenant_direct` and `overhead_allocated` are derived
    from the actual CostLineItem rows (the billing truth), not from
    CostCollectionRun.stats — stats are a single shared snapshot written by
    whichever path (collector or demo seed) last touched that hour, and
    historically drifted from what was actually billed once runs started
    being shared/reseeded across organizations. `unallocated_idle` is
    whatever's left of the ground truth after direct + allocated; `residual`
    only appears (and only ever goes negative) if direct + allocated somehow
    exceeds the ground truth, surfacing a real inconsistency instead of
    silently clamping it away.
    """
    stats_row = (
        db.session.query(
            func.sum(
                sa_cast(CostCollectionRun.stats["cluster_compute_cost"].astext, Float)
            ).label("cluster_compute"),
            func.sum(
                sa_cast(CostCollectionRun.stats["fixed_overhead"].astext, Float)
            ).label("fixed_overhead"),
        )
        .filter(
            CostCollectionRun.status == "succeeded",
            CostCollectionRun.period_start >= start,
            CostCollectionRun.period_start < end,
        )
        .one()
    )
    cluster_compute = float(stats_row.cluster_compute or 0.0)
    fixed_overhead = float(stats_row.fixed_overhead or 0.0)

    items_row = (
        db.session.query(
            func.sum(
                case(
                    (
                        CostLineItem.category.in_(
                            ("compute", "backing_service", "build")
                        ),
                        CostLineItem.cost_usd,
                    ),
                    else_=0.0,
                )
            ).label("tenant_direct"),
            func.sum(
                case(
                    (CostLineItem.category == "overhead", CostLineItem.cost_usd),
                    else_=0.0,
                )
            ).label("overhead_allocated"),
        )
        .filter(CostLineItem.period_start >= start, CostLineItem.period_start < end)
        .one()
    )
    tenant_direct = float(items_row.tenant_direct or 0.0)
    overhead_allocated = float(items_row.overhead_allocated or 0.0)

    total = cluster_compute + fixed_overhead
    raw_idle = total - tenant_direct - overhead_allocated
    unallocated_idle = max(raw_idle, 0.0)
    residual = min(raw_idle, 0.0)
    return {
        "cluster_compute": cluster_compute,
        "fixed_overhead": fixed_overhead,
        "tenant_direct": tenant_direct,
        "overhead_allocated": overhead_allocated,
        "unallocated_idle": unallocated_idle,
        "total": total,
        "residual": residual,
    }


def _platform_capacity_series(
    scope: CostScope,
    start: datetime.datetime,
    end: datetime.datetime,
    interval: Interval,
    buckets: list[datetime.datetime],
    rates: CostRates,
) -> CapacitySeries:
    """capacity_cost per bucket is a plain SUM over whatever succeeded hours
    exist in that bucket (never an average or a division by bucket-width),
    so a day mixing collector hours with missing/failed hours just shows
    less total capacity for that day rather than computing a misleading
    per-hour rate. Demo-seeded cluster_compute_cost is derived per-hour as a
    markup over that hour's actual billed tenant_direct (see
    demo.finalize_platform_stats), so capacity_cost is >= requested_cost by
    construction for seeded data.
    """
    trunc = func.date_trunc(interval, CostCollectionRun.period_start)
    rows = (
        db.session.query(
            trunc.label("bucket"),
            func.sum(
                sa_cast(CostCollectionRun.stats["cluster_compute_cost"].astext, Float)
            ).label("cost"),
        )
        .filter(
            CostCollectionRun.status == "succeeded",
            CostCollectionRun.period_start >= start,
            CostCollectionRun.period_start < end,
        )
        .group_by("bucket")
        .all()
    )
    by_bucket = {_as_utc(row.bucket): float(row.cost or 0.0) for row in rows}
    capacity_cost = [by_bucket.get(bucket, 0.0) for bucket in buckets]
    efficiency = _series_efficiency(scope, start, end, interval, buckets, rates)
    return {
        "capacity_cost": capacity_cost,
        "requested_cost": efficiency["request"],
        "used_cost": efficiency["usage"],
    }


def _platform_nodes(
    rates: CostRates, start: datetime.datetime, end: datetime.datetime
) -> list[NodeRow]:
    """Grouped by instance type: `node_hours` approximates every node in
    that group as present for the whole requested range (true per-node
    hourly presence isn't persisted — only the live collector's aggregate
    `node_hours` stat is, which isn't broken out by type).
    """
    hours_in_range = max((end - start).total_seconds() / 3600.0, 0.0)
    rows = (
        db.session.query(
            CostNode.instance_type,
            CostNode.capacity_type,
            # Labeled "node_count", not "count": SQLAlchemy Row objects
            # inherit tuple.count(), which would shadow a `.count` column
            # attribute.
            func.count().label("node_count"),
            func.avg(CostNode.cpu_capacity).label("cpu_capacity"),
            func.avg(CostNode.memory_gib).label("memory_gib"),
        )
        .filter(CostNode.instance_type.isnot(None))
        .group_by(CostNode.instance_type, CostNode.capacity_type)
        .all()
    )
    nodes: list[NodeRow] = []
    for row in rows:
        hourly = node_hourly_price(
            row.instance_type,
            rates,
            cpu_capacity=row.cpu_capacity,
            memory_gib=row.memory_gib,
        )
        node_count: int = row.node_count
        node_hours = node_count * hours_in_range
        nodes.append(
            {
                "instance_type": row.instance_type,
                "capacity_type": row.capacity_type,
                "count": node_count,
                "node_hours": node_hours,
                "hourly": hourly,
                "cost": node_hours * hourly,
            }
        )
    nodes.sort(key=lambda n: n["cost"], reverse=True)
    return nodes


def _platform_unallocated(
    start: datetime.datetime, end: datetime.datetime
) -> UnallocatedStats:
    # Averaged (not summed) over the range: pods_seen/pods_unallocated are
    # per-hour snapshot counts, so summing across e.g. 2160 hours would be
    # meaningless — the average hourly count is what's comparable to "how
    # many pods exist".
    row = (
        db.session.query(
            func.avg(
                sa_cast(CostCollectionRun.stats["pods_seen"].astext, Integer)
            ).label("pods_seen"),
            func.avg(
                sa_cast(CostCollectionRun.stats["pods_unallocated"].astext, Integer)
            ).label("pods_unallocated"),
        )
        .filter(
            CostCollectionRun.status == "succeeded",
            CostCollectionRun.period_start >= start,
            CostCollectionRun.period_start < end,
        )
        .one()
    )
    pods_seen = float(row.pods_seen or 0.0)
    pods_unallocated = float(row.pods_unallocated or 0.0)
    share = (pods_unallocated / pods_seen) if pods_seen else None
    return {
        "pods_seen": pods_seen,
        "pods_unallocated": pods_unallocated,
        "share": share,
    }


# ---------------------------------------------------------------------------
# Rates display
# ---------------------------------------------------------------------------


def _rates_display(rates: CostRates) -> RatesDisplay:
    line_items: list[RateLineItem] = [
        {
            "key": key,
            "label": label,
            "value": _simple_rates(rates).get(rate_key),
            "unit": unit,
            "note": note,
        }
        for key, label, rate_key, unit, note in RATE_LINE_ITEMS
    ]
    for key, label in FIXED_RATE_ITEMS:
        line_items.append(
            {
                "key": key,
                "label": label,
                "value": rates["fixed_hourly"].get(key),
                "unit": "USD/hr",
                "note": "fixed, shared across tenants on the cluster",
            }
        )

    families_in_use = {
        node.instance_type
        for node in CostNode.query.filter(CostNode.instance_type.isnot(None)).all()
    }
    if not families_in_use:
        families_in_use = {
            f"{family}.xlarge" for family in rates["node_family_xlarge_hourly"]
        }
    nodes: list[RatesNode] = [
        {
            "instance_type": instance_type,
            "hourly": node_hourly_price(instance_type, rates),
        }
        for instance_type in sorted(families_in_use)
        if instance_type is not None
    ]

    return {"line_items": line_items, "nodes": nodes}


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def _freshness(
    start: datetime.datetime, end: datetime.datetime, now: datetime.datetime
) -> FreshnessStats:
    collection_enabled = bool(current_app.config.get("SHOW_COST_ESTIMATES"))

    last_run: CostCollectionRun | None = CostCollectionRun.query.order_by(
        CostCollectionRun.started_at.desc()
    ).first()
    last_succeeded: CostCollectionRun | None = (
        CostCollectionRun.query.filter_by(status="succeeded")
        .order_by(CostCollectionRun.period_start.desc())
        .first()
    )
    data_through = (
        last_succeeded.period_start + datetime.timedelta(hours=1)
        if last_succeeded is not None
        else None
    )
    stale = data_through is None or (now - data_through) > STALE_AFTER

    current_hour = now.replace(minute=0, second=0, microsecond=0)
    if now.minute < COLLECTION_GRACE_MINUTES:
        # The hour that just completed hasn't had its collection run yet.
        collection_cutoff = current_hour - datetime.timedelta(hours=1)
    else:
        collection_cutoff = current_hour
    range_end_hour = min(end, collection_cutoff)
    range_start_hour = start.replace(minute=0, second=0, microsecond=0)
    expected_hours: list[datetime.datetime] = []
    cursor = range_start_hour
    while cursor < range_end_hour:
        expected_hours.append(cursor)
        cursor += datetime.timedelta(hours=1)

    runs_in_range: dict[datetime.datetime, CostCollectionRun] = {
        run.period_start: run
        for run in CostCollectionRun.query.filter(
            CostCollectionRun.period_start >= range_start_hour,
            CostCollectionRun.period_start < range_end_hour,
        ).all()
    }
    missing_hours = [hour for hour in expected_hours if hour not in runs_in_range]
    # A run's own `status` only describes the data it currently holds, which
    # may be older data preserved from a prior successful attempt (see
    # collector._record_attempt) -- `stats.last_attempt_status` reflects
    # what the most recent attempt for that hour actually did, so an hour
    # counts as failed here even if its held data (and `status`) still say
    # succeeded.
    failed_hours = sorted(
        hour
        for hour, run in runs_in_range.items()
        if run.status == "failed"
        or (run.stats or {}).get("last_attempt_status") == "failed"
    )

    # Currently-failing streak: starting from the single most recently
    # attempted hour install-wide (not just within this report's range),
    # walk back hour by hour while each hour's last recorded attempt also
    # failed, to find when the ongoing failure started.
    currently_failing = False
    failing_since: str | None = None
    latest_attempt_run: CostCollectionRun | None = (
        CostCollectionRun.query.filter(
            CostCollectionRun.stats["last_attempt_at"].isnot(None)
        )
        .order_by(CostCollectionRun.stats["last_attempt_at"].astext.desc())
        .first()
    )
    if latest_attempt_run is not None:
        latest_stats = latest_attempt_run.stats or {}
        if latest_stats.get("last_attempt_status") == "failed":
            currently_failing = True
            failing_since = latest_stats.get("last_attempt_at")
            cursor = latest_attempt_run.period_start
            for _ in range(FAILING_STREAK_LOOKBACK_HOURS):
                prev_hour = cursor - datetime.timedelta(hours=1)
                prev_run: CostCollectionRun | None = CostCollectionRun.query.filter_by(
                    period_start=prev_hour
                ).first()
                prev_stats: CostRunStats = (prev_run.stats or {}) if prev_run else {}
                if prev_stats.get("last_attempt_status") != "failed":
                    break
                if prev_attempt_at := prev_stats.get("last_attempt_at"):
                    failing_since = prev_attempt_at
                cursor = prev_hour

    return {
        "collection_enabled": collection_enabled,
        "last_run_at": last_run.started_at.isoformat()
        if last_run is not None
        else None,
        "data_through": data_through.isoformat() if data_through is not None else None,
        "stale": stale,
        "missing_hours": len(missing_hours),
        "failed_hours": [hour.isoformat() for hour in failed_hours],
        "currently_failing": currently_failing,
        "failing_since": failing_since,
    }
