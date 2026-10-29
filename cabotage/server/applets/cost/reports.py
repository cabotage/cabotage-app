"""Cost report assembly — the UI's data contract.

``build_report(scope, range_key, now=None, start=None, end=None)`` aggregates
``CostLineItem`` rows (already priced and categorized by the collector) into
the JSON shape the cost dashboard renders. Pages read these snapshots only;
this module never queries Mimir or Kubernetes.
"""

import bisect
import datetime
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from flask import current_app
from sqlalchemy import ColumnElement, Float, Integer, Row, case, func
from sqlalchemy import cast as sa_cast
from sqlalchemy.orm import InstrumentedAttribute, Query, joinedload

from cabotage.server import db
from cabotage.server.applets.cost.constants import (
    ACTUALS_BUCKET_LABELS,
    CALENDAR_SPAN,
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    COLLECTION_GRACE_MINUTES,
    CUSTOM_HOURLY_SPAN,
    DEFAULT_RATES,
    FIXED_RATE_LABELS,
    MAX_EXTENT_BUCKETS,
    METER_LABELS,
    METER_ORDER,
    METER_RATE_KEY,
    METER_UNITS,
    PRIMARY_BREAKDOWN,
    RANGE_CONFIG,
    RATE_LINE_ITEMS,
    STALE_AFTER,
    TOP_N_BREAKDOWN_SERIES,
    WEEKDAY_DOW,
    WEEKDAY_LABELS,
)
from cabotage.server.applets.cost.helpers import as_utc
from cabotage.server.applets.cost.rates import effective_rates, node_hourly_price
from cabotage.server.applets.cost.types import (
    AwsBucketRow,
    AwsReconciliation,
    BreakdownAggregates,
    BreakdownDimension,
    BreakdownKey,
    BreakdownRow,
    BreakdownSlugs,
    CapacitySeries,
    CostRates,
    EfficiencySeries,
    FreshnessStats,
    HeatmapPayload,
    Interval,
    MeterRow,
    MoverRow,
    NameResolver,
    NodeInventoryStats,
    NodeRow,
    PlatformBlock,
    PlatformComponentRow,
    PriceTier,
    RateLineItem,
    RatesDisplay,
    RatesNode,
    ReconciliationStats,
    ResolvedName,
    SeriesBreakdownEntry,
    SeriesPayload,
    UnallocatedStats,
)
from cabotage.server.models.auth import Organization
from cabotage.server.models.cost import (
    CostAwsDaily,
    CostCollectionRun,
    CostLineItem,
    CostNode,
    CostNodeHour,
)
from cabotage.server.models.projects import Application, Environment, Project
from cabotage.server.models.resources import Resource

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


def _simple_rates(rates: CostRates) -> dict[str, float]:
    """The 5 per-unit rate keys as a plain dict, so callers can look one up
    by a dynamic (non-literal) key without indexing the ``CostRates``
    TypedDict directly -- TypedDict subscripting requires a literal key.
    """
    return {
        "vcpu_hour": rates["vcpu_hour"],
        "memory_gib_hour": rates["memory_gib_hour"],
        "storage_gib_month": rates["storage_gib_month"],
        "egress_gb": rates["egress_gb"],
        "network_gb": rates["network_gb"],
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


def _meter_rows(
    rates: CostRates, totals: Mapping[str, tuple[float, float]]
) -> list[MeterRow]:
    rows: list[MeterRow] = []
    for meter in METER_ORDER:
        quantity, cost = totals.get(meter, (0.0, 0.0))
        # Storage cost includes IOPS and throughput, so show cost per GiB-mo.
        rate = (
            cost / quantity
            if meter == "storage" and quantity
            else _rate_for_meter_optional(rates, meter)
        )
        rows.append(
            {
                "key": meter,
                "label": METER_LABELS[meter],
                "unit": METER_UNITS[meter],
                "quantity": quantity,
                "rate": rate,
                "cost": cost,
            }
        )
    return rows


@dataclass
class CostScope:
    kind: str
    # Every kind except "platform" (admin-only, whole-install view) needs an
    # organization; platform aggregates across all of them.
    organization: Organization | None = None
    project: Project | None = None
    environment: Environment | None = None
    application: Application | None = None


def build_report(
    scope: CostScope,
    range_key: str,
    now: datetime.datetime | None = None,
    start: datetime.datetime | str | None = None,
    end: datetime.datetime | str | None = None,
) -> dict[str, object]:
    now = as_utc(now or datetime.datetime.now(datetime.UTC))
    rates = effective_rates()

    first_hour, last_hour = _extent()

    if start is not None or end is not None:
        range_start, range_end, interval, label = _resolve_custom_range(
            start, end, now, first_hour
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
    completed_hours = int(elapsed_hours)
    covered_hours = min(_covered_hours(month_start, now), completed_hours)
    uncovered_hours = completed_hours - covered_hours

    run_rate_hourly = _run_rate_hourly(scope, now)
    projected_month = (
        mtd_cost + run_rate_hourly * (remaining_hours + uncovered_hours)
        if run_rate_hourly is not None
        else None
    )
    run_rate_monthly = (
        run_rate_hourly * hours_in_month if run_rate_hourly is not None else None
    )
    efficiency = _efficiency(scope, range_start, range_end, rates)

    buckets = _bucket_starts(range_start, range_end, interval)
    series = _series(scope, range_start, range_end, interval, buckets)
    series["previous"] = _series_previous(
        scope, previous_start, previous_end, buckets, duration
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
        scope, range_start, range_end, interval, buckets, raw_keys_by_name, breakdowns
    )

    for name, rows in breakdowns.items():
        column, category_filter = _BREAKDOWN_DIMENSIONS[name]
        raw_keys = raw_keys_by_name[name]
        top_keys = raw_keys[:TOP_N_BREAKDOWN_SERIES]
        meters_by_key = _breakdown_detail(
            scope,
            column,
            category_filter,
            top_keys,
            range_start,
            range_end,
            rates,
        )
        top_key_set = set(top_keys)
        for row, raw_key in zip(rows, raw_keys, strict=True):
            row["meters"] = (
                meters_by_key.get(raw_key, []) if raw_key in top_key_set else None
            )

    primary_name = PRIMARY_BREAKDOWN[scope.kind]
    movers = _movers(breakdowns[primary_name])

    heatmap = _heatmap(scope, range_start, range_end)
    freshness = _freshness(
        range_start, range_end, now, first_hour, scope.kind == "platform"
    )

    extent_series = _extent_series(scope, first_hour, last_hour)
    calendar = _calendar_series(scope, first_hour, last_hour)
    rates_display = _rates_display(rates, range_start, scope.kind == "platform")

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
            "nodes": _platform_nodes(range_start, range_end),
            "node_inventory": _platform_node_inventory(),
            "unallocated": _platform_unallocated(range_start, range_end),
            "top_orgs_series": series["breakdowns"].get(
                "organizations", empty_breakdown
            ),
            "platform_components": _platform_components(range_start, range_end),
        }

    kpis: dict[str, object] = {
        "month_to_date": mtd_cost,
        "projected_month": projected_month,
        "run_rate_monthly": run_rate_monthly,
        "efficiency": efficiency,
    }
    if scope.kind == "platform":
        # Platform only: this is collector health.
        kpis["coverage"] = {
            "covered_hours": covered_hours,
            "completed_hours": completed_hours,
        }

    return {
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
        "kpis": kpis,
        "categories": [
            {
                "key": category,
                "label": CATEGORY_LABELS[category],
                "cost": totals.get(category, 0.0),
                "share": (totals.get(category, 0.0) / cost) if cost else 0.0,
            }
            for category in CATEGORY_ORDER
        ],
        "meters": _meter_rows(rates, meters),
        "series": series,
        "breakdowns": breakdowns,
        "freshness": freshness,
        "rates": rates_display,
        "rates_source": rates.get("source", DEFAULT_RATES.get("source")),
        "extent_series": extent_series,
        "calendar": calendar,
        "heatmap": heatmap,
        "movers": movers,
        "platform": platform,
    }


# ---------------------------------------------------------------------------
# Time ranges
# ---------------------------------------------------------------------------


def _utc_trunc(
    interval: Interval, column: InstrumentedAttribute[datetime.datetime]
) -> ColumnElement[datetime.datetime]:
    """``date_trunc`` in UTC, independent of the session TimeZone."""
    return func.date_trunc(interval, func.timezone("UTC", column))


def _parse_range_param(value: datetime.datetime | str) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return as_utc(value)
    parsed = datetime.datetime.fromisoformat(value)
    return as_utc(parsed)


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
) -> tuple[datetime.datetime, datetime.datetime, Interval, str]:
    range_start = (
        _parse_range_param(start) if start is not None else (first_hour or now)
    )
    range_end = _parse_range_param(end) if end is not None else now
    range_end = min(range_end, now)

    # Clamp the low end to the start of available data; clamp the high end
    # only to "now", so a requested range can still reach into the most
    # recent, possibly-not-yet-collected, in-progress hour. With no data
    # collected yet (first_hour is None) there's nothing to clamp against,
    # so fall back to the default 90-day window -- otherwise a `start` years
    # in the past would make `_freshness`/bucketing iterate empty hours by
    # the thousands.
    if first_hour is not None:
        range_start = max(range_start, first_hour)
    else:
        range_start = max(range_start, range_end - RANGE_CONFIG["90d"]["duration"])
    if range_end <= range_start:
        range_end = range_start + datetime.timedelta(hours=1)

    span = range_end - range_start
    interval: Interval = "hour" if span <= CUSTOM_HOURLY_SPAN else "day"
    label = f"{range_start.date().isoformat()} – {range_end.date().isoformat()}"
    return range_start, range_end, interval, label


def _bucket_starts(
    start: datetime.datetime, end: datetime.datetime, interval: Interval
) -> list[datetime.datetime]:
    # Hours ceil to `start`; days/weeks floor so a partial first bucket keeps its rows.
    if interval == "hour":
        cursor = start.replace(minute=0, second=0, microsecond=0)
        if cursor < start:
            cursor += datetime.timedelta(hours=1)
        step = datetime.timedelta(hours=1)
    elif interval == "week":
        cursor = (start - datetime.timedelta(days=start.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        step = datetime.timedelta(weeks=1)
    else:
        cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
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
    trunc = _utc_trunc(interval, CostLineItem.period_start)
    query = _scope_filter(
        db.session.query(
            trunc.label("bucket"),
            CostLineItem.category,
            func.sum(CostLineItem.cost_usd).label("cost"),
        ),
        scope,
    ).filter(CostLineItem.period_start >= start, CostLineItem.period_start < end)
    rows = query.group_by("bucket", CostLineItem.category).all()

    by_bucket: defaultdict[datetime.datetime, defaultdict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    for row in rows:
        bucket_dt = as_utc(row.bucket)
        by_bucket[bucket_dt][row.category] += float(row.cost)

    timestamps = [int(bucket.timestamp()) for bucket in buckets]
    categories = {
        category: [by_bucket[bucket][category] for bucket in buckets]
        for category in CATEGORY_ORDER
    }
    return {"timestamps": timestamps, "categories": categories}


def _series_previous(
    scope: CostScope,
    previous_start: datetime.datetime,
    previous_end: datetime.datetime,
    buckets: list[datetime.datetime],
    shift: datetime.timedelta,
) -> list[float]:
    """Previous-period totals, shifted onto the current range's buckets."""
    values = [0.0] * len(buckets)
    if not buckets:
        return values
    query = _scope_filter(
        db.session.query(
            CostLineItem.period_start.label("period_start"),
            func.sum(CostLineItem.cost_usd).label("cost"),
        ),
        scope,
    ).filter(
        CostLineItem.period_start >= previous_start,
        CostLineItem.period_start < previous_end,
    )
    for row in query.group_by(CostLineItem.period_start).all():
        index = bisect.bisect_right(buckets, as_utc(row.period_start) + shift) - 1
        values[max(index, 0)] += float(row.cost)
    return values


def _series_efficiency(
    scope: CostScope,
    start: datetime.datetime,
    end: datetime.datetime,
    interval: Interval,
    buckets: list[datetime.datetime],
    rates: CostRates,
) -> EfficiencySeries:
    trunc = _utc_trunc(interval, CostLineItem.period_start)
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
    usage_by_bucket: defaultdict[datetime.datetime, float] = defaultdict(float)
    request_by_bucket: defaultdict[datetime.datetime, float] = defaultdict(float)
    billed_by_bucket: defaultdict[datetime.datetime, float] = defaultdict(float)
    for row in query.group_by("bucket", CostLineItem.meter).all():
        bucket = as_utc(row.bucket)
        rate = _rate_for_meter(rates, row.meter)
        usage_by_bucket[bucket] += float(row.usage or 0.0) * rate
        request_by_bucket[bucket] += float(row.request or 0.0) * rate
        billed_by_bucket[bucket] += float(row.billed or 0.0) * rate
    return {
        "usage": [usage_by_bucket[bucket] for bucket in buckets],
        "request": [request_by_bucket[bucket] for bucket in buckets],
        "billed": [billed_by_bucket[bucket] for bucket in buckets],
    }


def _series_breakdowns(
    scope: CostScope,
    start: datetime.datetime,
    end: datetime.datetime,
    interval: Interval,
    buckets: list[datetime.datetime],
    raw_keys_by_name: dict[str, list[BreakdownKey]],
    breakdowns: dict[str, list[BreakdownRow]],
) -> dict[str, SeriesBreakdownEntry]:
    trunc = _utc_trunc(interval, CostLineItem.period_start)
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
        values_by_key: dict[BreakdownKey, defaultdict[datetime.datetime, float]] = {
            key: defaultdict(float) for key in top_keys
        }
        other_by_bucket: defaultdict[datetime.datetime, float] = defaultdict(float)
        for row in query.group_by("bucket", "key").all():
            bucket = as_utc(row.bucket)
            if row.key in top_key_set:
                values_by_key[row.key][bucket] += float(row.cost)
            else:
                other_by_bucket[bucket] += float(row.cost)

        names = {row["key"]: row["name"] for row in breakdowns[name]}
        result[name] = {
            "keys": [str(key) for key in top_keys],
            "names": {str(key): names.get(str(key), str(key)) for key in top_keys},
            "values": {
                str(key): [values_by_key[key][bucket] for bucket in buckets]
                for key in top_keys
            },
            "other": [other_by_bucket[bucket] for bucket in buckets],
        }
    return result


def _breakdown_detail(
    scope: CostScope,
    column: InstrumentedAttribute[BreakdownKey | None],
    category_filter: list[str] | None,
    keys: list[BreakdownKey],
    start: datetime.datetime,
    end: datetime.datetime,
    rates: CostRates,
) -> dict[BreakdownKey, list[MeterRow]]:
    if not keys:
        return {}

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
    totals_by_key: defaultdict[BreakdownKey, dict[str, tuple[float, float]]] = (
        defaultdict(dict)
    )
    for row in totals_query.group_by("key", CostLineItem.meter).all():
        totals_by_key[row.key][row.meter] = (float(row.quantity), float(row.cost))

    meters_by_key: dict[BreakdownKey, list[MeterRow]] = {}
    for key in keys:
        meters_by_key[key] = _meter_rows(rates, totals_by_key[key])

    return meters_by_key


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


def _covered_hours(start: datetime.datetime, end: datetime.datetime) -> int:
    """Hours with a succeeded run."""
    end_hour = end.replace(minute=0, second=0, microsecond=0)
    hours: int = CostCollectionRun.query.filter(
        CostCollectionRun.status == "succeeded",
        CostCollectionRun.period_start >= start,
        CostCollectionRun.period_start < end_hour,
    ).count()
    return hours


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
    "processes": (CostLineItem.component, ["compute", "egress", "network"]),
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

    categories_by_key: defaultdict[BreakdownKey, defaultdict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    totals_by_key: defaultdict[BreakdownKey, float] = defaultdict(float)
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
    efficiency_num: defaultdict[BreakdownKey, float] = defaultdict(float)
    efficiency_den: defaultdict[BreakdownKey, float] = defaultdict(float)
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
    totals_by_key: defaultdict[BreakdownKey, float],
    categories_by_key: defaultdict[BreakdownKey, defaultdict[str, float]],
    previous_by_key: dict[BreakdownKey, float],
    efficiency_num: defaultdict[BreakdownKey, float],
    efficiency_den: defaultdict[BreakdownKey, float],
    name_resolver: NameResolver,
) -> tuple[list[BreakdownRow], list[BreakdownKey]]:
    total_cost = sum(totals_by_key.values())
    rows: list[BreakdownRow] = []
    row_costs: list[float] = []
    raw_keys_tmp: list[BreakdownKey] = []
    keys = list(totals_by_key) + [
        key for key in previous_by_key if key not in totals_by_key
    ]
    resolved_by_key = name_resolver(keys)
    for key in keys:
        cost = totals_by_key.get(key, 0.0)
        resolved = resolved_by_key.get(key)
        if resolved is None:
            continue
        name, context, slugs, deleted = resolved
        efficiency = (
            efficiency_num[key] / efficiency_den[key] if efficiency_den[key] else None
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
                    category: categories_by_key[key][category]
                    for category in CATEGORY_ORDER
                },
                "efficiency": efficiency,
                "slugs": slugs,
                "deleted": deleted,
                "meters": None,
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

    def resolver(keys: list[BreakdownKey]) -> dict[BreakdownKey, ResolvedName]:
        organizations = {
            org.id: org
            for org in Organization.query.filter(Organization.id.in_(keys)).all()
        }
        resolved: dict[BreakdownKey, ResolvedName] = {}
        for key in keys:
            organization = organizations.get(key)
            if organization is None:
                resolved[key] = (
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
                continue
            resolved[key] = (
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
        return resolved

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

    def resolver(keys: list[BreakdownKey]) -> dict[BreakdownKey, ResolvedName]:
        found: list[Project] = (
            Project.query.filter(Project.id.in_(keys))
            .options(joinedload(Project.organization))
            .all()
        )
        projects: dict[BreakdownKey, Project] = {
            project.id: project for project in found
        }
        resolved: dict[BreakdownKey, ResolvedName] = {}
        for key in keys:
            project = projects.get(key)
            if project is None:
                resolved[key] = (
                    "(deleted project)",
                    None,
                    {"project": None, "environment": None, "application": None},
                    True,
                )
                continue
            # Platform scope spans every organization, so the context line
            # and link need to identify which org this project belongs to.
            context = project.organization.name if scope.kind == "platform" else None
            slugs: BreakdownSlugs = {
                "project": project.slug,
                "environment": None,
                "application": None,
            }
            if scope.kind == "platform":
                slugs["organization"] = project.organization.slug
            resolved[key] = (
                project.name,
                context,
                slugs,
                project.deleted_at is not None,
            )
        return resolved

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

    def resolver(keys: list[BreakdownKey]) -> dict[BreakdownKey, ResolvedName]:
        found: list[Environment] = (
            Environment.query.filter(Environment.id.in_(keys))
            .options(joinedload(Environment.project))
            .all()
        )
        environments: dict[BreakdownKey, Environment] = {
            environment.id: environment for environment in found
        }
        resolved: dict[BreakdownKey, ResolvedName] = {}
        for key in keys:
            environment = environments.get(key)
            if environment is None:
                resolved[key] = (
                    "(deleted environment)",
                    None,
                    {"project": None, "environment": None, "application": None},
                    True,
                )
                continue
            project = environment.project
            resolved[key] = (
                environment.name,
                project.name if project is not None else None,
                {
                    "project": project.slug if project is not None else None,
                    "environment": environment.slug,
                    "application": None,
                },
                environment.deleted_at is not None,
            )
        return resolved

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

    def resolver(keys: list[BreakdownKey]) -> dict[BreakdownKey, ResolvedName]:
        found: list[Application] = (
            Application.query.filter(Application.id.in_(keys))
            .options(joinedload(Application.project).joinedload(Project.organization))
            .all()
        )
        applications: dict[BreakdownKey, Application] = {
            application.id: application for application in found
        }
        resolved: dict[BreakdownKey, ResolvedName] = {}
        for key in keys:
            application = applications.get(key)
            if application is None:
                resolved[key] = (
                    "(deleted application)",
                    None,
                    {"project": None, "environment": None, "application": None},
                    True,
                )
                continue
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
            resolved[key] = (
                application.name,
                context,
                slugs,
                application.deleted_at is not None,
            )
        return resolved

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

    def resolver(keys: list[BreakdownKey]) -> dict[BreakdownKey, ResolvedName]:
        found: list[Resource] = (
            Resource.query.filter(Resource.id.in_(keys))
            .options(joinedload(Resource.environment).joinedload(Environment.project))
            .all()
        )
        resources: dict[BreakdownKey, Resource] = {
            resource.id: resource for resource in found
        }
        resolved: dict[BreakdownKey, ResolvedName] = {}
        for key in keys:
            resource = resources.get(key)
            if resource is None:
                resolved[key] = (
                    "(deleted resource)",
                    None,
                    {"project": None, "environment": None, "application": None},
                    True,
                )
                continue
            environment = resource.environment
            project = environment.project if environment is not None else None
            resolved[key] = (
                resource.name,
                resource.type,
                {
                    "project": project.slug if project is not None else None,
                    "environment": environment.slug
                    if environment is not None
                    else None,
                    "application": None,
                },
                resource.deleted_at is not None,
            )
        return resolved

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

    def resolver(keys: list[BreakdownKey]) -> dict[BreakdownKey, ResolvedName]:
        return {
            key: (
                str(key),
                None,
                {
                    "project": project_slug,
                    "environment": None,
                    "application": application_slug,
                },
                False,
            )
            for key in keys
        }

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
        movers.append(
            {
                "key": row["key"],
                "name": row["name"],
                "context": row["context"],
                "cost": row["cost"],
                "change": change,
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
        return _empty_series()
    span_days = (last_hour - first_hour).total_seconds() / 86400.0
    interval: Interval = "day" if span_days <= MAX_EXTENT_BUCKETS else "week"
    buckets = _bucket_starts(first_hour, last_hour, interval)
    return _series(scope, first_hour, last_hour, interval, buckets)


def _calendar_series(
    scope: CostScope,
    first_hour: datetime.datetime | None,
    last_hour: datetime.datetime | None,
) -> SeriesPayload:
    if first_hour is None or last_hour is None:
        return _empty_series()
    start = max(first_hour, last_hour - CALENDAR_SPAN)
    return _series(
        scope, start, last_hour, "day", _bucket_starts(start, last_hour, "day")
    )


def _empty_series() -> SeriesPayload:
    return {
        "timestamps": [],
        "categories": {category: [] for category in CATEGORY_ORDER},
    }


# ---------------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------------


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
            func.extract("dow", func.timezone("UTC", hour_totals.c.period_start)).label(
                "dow"
            ),
            func.extract(
                "hour", func.timezone("UTC", hour_totals.c.period_start)
            ).label("hour"),
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

    Only hours with node data count, and true-up rows are left out.
    """
    node_runs = (
        db.session.query(
            CostCollectionRun.period_start,
            sa_cast(
                CostCollectionRun.stats["cluster_compute_cost"].astext, Float
            ).label("cluster_compute"),
            sa_cast(CostCollectionRun.stats["fixed_overhead"].astext, Float).label(
                "fixed_overhead"
            ),
        )
        .filter(
            CostCollectionRun.status == "succeeded",
            CostCollectionRun.period_start >= start,
            CostCollectionRun.period_start < end,
            func.jsonb_extract_path_text(CostCollectionRun.stats, "node_data_available")
            == "true",
        )
        .all()
    )
    hours = [row.period_start for row in node_runs]
    cluster_compute = sum(float(row.cluster_compute or 0.0) for row in node_runs)
    fixed_overhead = sum(float(row.fixed_overhead or 0.0) for row in node_runs)

    tenant_direct = 0.0
    overhead_allocated = 0.0
    if hours:
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
            .filter(
                CostLineItem.period_start.in_(hours),
                CostLineItem.run_id.isnot(None),
            )
            .one()
        )
        tenant_direct = float(items_row.tenant_direct or 0.0)
        overhead_allocated = float(items_row.overhead_allocated or 0.0)

    total = cluster_compute + fixed_overhead
    raw_idle = total - tenant_direct - overhead_allocated
    result: ReconciliationStats = {
        "hours": len(hours),
        "cluster_compute": cluster_compute,
        "fixed_overhead": fixed_overhead,
        "tenant_direct": tenant_direct,
        "overhead_allocated": overhead_allocated,
        "unallocated_idle": max(raw_idle, 0.0),
        "total": total,
        "residual": min(raw_idle, 0.0),
    }
    aws = _aws_reconciliation(start, end)
    if aws is not None:
        result["aws"] = aws
    return result


def _aws_reconciliation(
    start: datetime.datetime, end: datetime.datetime
) -> AwsReconciliation | None:
    """Actuals vs. everything allocated, over whole days with actuals and
    collected hours. Each day's actuals are scaled to the hours collected."""
    first_day = start.date()
    if start.time() != datetime.time():
        first_day += datetime.timedelta(days=1)
    end_day = end.date()  # exclusive: a partial last day isn't compared
    run_day = func.date(func.timezone("UTC", CostCollectionRun.period_start))
    hours_by_day: dict[datetime.date, int] = {
        day: count
        for day, count in db.session.query(run_day, func.count())
        .filter(
            CostCollectionRun.status == "succeeded",
            run_day >= first_day,
            run_day < end_day,
        )
        .group_by(run_day)
        .all()
    }
    bucket_costs: defaultdict[str, float] = defaultdict(float)
    day_set: set[datetime.date] = set()
    for day, bucket, cost in (
        db.session.query(CostAwsDaily.day, CostAwsDaily.bucket, CostAwsDaily.cost_usd)
        .filter(CostAwsDaily.day >= first_day, CostAwsDaily.day < end_day)
        .all()
    ):
        hours = hours_by_day.get(day, 0)
        if not hours:
            continue
        bucket_costs[bucket] += float(cost) * hours / 24
        day_set.add(day)
    if not day_set:
        return None
    days = sorted(day_set)
    line_day = func.date(func.timezone("UTC", CostLineItem.period_start))
    allocated_row = (
        db.session.query(
            func.coalesce(func.sum(CostLineItem.cost_usd), 0).label("allocated"),
            func.coalesce(
                func.sum(
                    case(
                        (CostLineItem.aws_day.isnot(None), CostLineItem.cost_usd),
                        else_=0,
                    )
                ),
                0,
            ).label("true_up"),
        )
        .filter(line_day.in_(days))
        .one()
    )
    actual = sum(bucket_costs.values())
    allocated = float(allocated_row.allocated or 0.0)
    buckets: list[AwsBucketRow] = [
        {
            "key": bucket,
            "label": ACTUALS_BUCKET_LABELS.get(bucket, bucket),
            "cost": cost,
        }
        for bucket, cost in bucket_costs.items()
    ]
    buckets.sort(key=lambda bucket: bucket["cost"], reverse=True)
    return {
        "days": len(days),
        "first_day": days[0].isoformat(),
        "last_day": days[-1].isoformat(),
        "actual": actual,
        "allocated": allocated,
        "true_up": float(allocated_row.true_up or 0.0),
        "difference": actual - allocated,
        "buckets": buckets,
    }


def _platform_components(
    start: datetime.datetime, end: datetime.datetime
) -> list[PlatformComponentRow]:
    """Sum of each run's ``stats.platform_components`` over the range."""
    fields = (
        "cpu_billed_hours",
        "memory_billed_gib_hours",
        "network_gb",
        "storage_gib_month",
        "cost_usd",
    )
    totals: defaultdict[str, dict[str, float]] = defaultdict(
        lambda: dict.fromkeys(fields, 0.0)
    )
    rows: list[Row[tuple[dict[str, dict[str, float]] | None]]] = (
        db.session.query(
            func.jsonb_extract_path(CostCollectionRun.stats, "platform_components")
        )
        .filter(
            CostCollectionRun.status == "succeeded",
            CostCollectionRun.period_start >= start,
            CostCollectionRun.period_start < end,
        )
        .all()
    )
    for row in rows:
        components: dict[str, dict[str, float]] | None = row[0]
        if not components:
            continue
        for name, component in components.items():
            entry = totals[name]
            for field in fields:
                entry[field] += float(component.get(field) or 0.0)
    grand_total = sum(entry["cost_usd"] for entry in totals.values())
    result: list[PlatformComponentRow] = [
        {
            "name": name,
            "cpu_billed_hours": entry["cpu_billed_hours"],
            "memory_billed_gib_hours": entry["memory_billed_gib_hours"],
            "network_gb": entry["network_gb"],
            "storage_gib_month": entry["storage_gib_month"],
            "cost": entry["cost_usd"],
            "share": entry["cost_usd"] / grand_total if grand_total else 0.0,
        }
        for name, entry in totals.items()
    ]
    result.sort(key=lambda row: row["cost"], reverse=True)
    return result


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
    trunc = _utc_trunc(interval, CostCollectionRun.period_start)
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
    by_bucket = {as_utc(row.bucket): float(row.cost or 0.0) for row in rows}
    capacity_cost = [by_bucket.get(bucket, 0.0) for bucket in buckets]
    efficiency = _series_efficiency(scope, start, end, interval, buckets, rates)
    return {
        "capacity_cost": capacity_cost,
        "requested_cost": efficiency["request"],
        "used_cost": efficiency["usage"],
    }


def _platform_nodes(start: datetime.datetime, end: datetime.datetime) -> list[NodeRow]:
    """Every node that ran pods in the range, from the collector's per-node
    hours: load is hour-weighted averages, dollars are summed.

    A row is a node name plus the type and capacity it was collected with,
    so a name reused by a different machine (EKS ``ip-…`` names follow
    recycled IPs) gets its own row instead of blending two machines, and
    hours collected while the node was unpriced show apart from priced ones.
    """
    rows: list[
        Row[
            tuple[
                str,
                str | None,
                str | None,
                float | None,
                float | None,
                float,
                float,
                float,
                float,
                float,
                Decimal | None,
                Decimal | None,
                Decimal | None,
                Decimal | None,
            ]
        ]
    ] = (
        db.session.query(
            CostNodeHour.node_name,
            CostNodeHour.instance_type,
            CostNodeHour.capacity_type,
            CostNodeHour.cpu_capacity,
            CostNodeHour.memory_gib,
            func.sum(CostNodeHour.node_hours).label("node_hours"),
            func.sum(CostNodeHour.cpu_request_hours).label("cpu_request_hours"),
            func.sum(CostNodeHour.cpu_usage_hours).label("cpu_usage_hours"),
            func.sum(CostNodeHour.memory_request_gib_hours).label("mem_request"),
            func.sum(CostNodeHour.memory_usage_gib_hours).label("mem_usage"),
            func.sum(CostNodeHour.cost_usd).label("cost"),
            func.sum(CostNodeHour.requested_cost_usd).label("requested_cost"),
            func.sum(CostNodeHour.used_cost_usd).label("used_cost"),
            func.sum(CostNodeHour.idle_cost_usd).label("idle_cost"),
        )
        .filter(CostNodeHour.period_start >= start, CostNodeHour.period_start < end)
        .group_by(
            CostNodeHour.node_name,
            CostNodeHour.instance_type,
            CostNodeHour.capacity_type,
            CostNodeHour.cpu_capacity,
            CostNodeHour.memory_gib,
        )
        .all()
    )

    nodes: list[NodeRow] = []
    for row in rows:
        node_hours = float(row.node_hours or 0.0)
        # Per-hour averages; node_hours is never zero for a stored row.
        per_hour = 1.0 / node_hours if node_hours else 0.0
        cost = float(row.cost) if row.cost is not None else None
        requested_cost = (
            float(row.requested_cost) if row.requested_cost is not None else None
        )
        used_cost = float(row.used_cost) if row.used_cost is not None else None
        idle_cost = float(row.idle_cost) if row.idle_cost is not None else None
        nodes.append(
            {
                "name": row.node_name,
                "instance_type": row.instance_type,
                "capacity_type": row.capacity_type,
                "cpu_capacity": row.cpu_capacity,
                "memory_gib": row.memory_gib,
                "node_hours": node_hours,
                "cpu_requested": float(row.cpu_request_hours or 0.0) * per_hour,
                "cpu_used": float(row.cpu_usage_hours or 0.0) * per_hour,
                "memory_requested": float(row.mem_request or 0.0) * per_hour,
                "memory_used": float(row.mem_usage or 0.0) * per_hour,
                "cost": cost,
                "requested_cost": requested_cost,
                "used_cost": used_cost,
                "idle_cost": idle_cost,
            }
        )
    nodes.sort(key=lambda n: (n["cost"] is None, -(n["cost"] or 0.0), n["name"]))
    return nodes


def _platform_node_inventory() -> NodeInventoryStats:
    latest: CostCollectionRun | None = (
        CostCollectionRun.query.filter_by(status="succeeded")
        .order_by(CostCollectionRun.period_start.desc())
        .first()
    )
    stats = (latest.stats or {}) if latest is not None else {}
    return {
        "period_start": latest.period_start.isoformat() if latest is not None else None,
        "error": stats.get("node_inventory_error"),
        "nodes_unpriced": int(stats.get("nodes_unpriced") or 0),
    }


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


def _rates_display(
    rates: CostRates, start: datetime.datetime, platform: bool
) -> RatesDisplay:
    volume_type = (
        rates["storage_volume"]["volume_type"] if "storage_volume" in rates else "gp3"
    )
    line_items: list[RateLineItem] = [
        {
            "key": key,
            "label": f"{label} ({volume_type})" if key == "storage" else label,
            "value": _simple_rates(rates).get(rate_key),
            "unit": unit,
            "note": note,
        }
        for key, label, rate_key, unit, note in RATE_LINE_ITEMS
    ]
    line_items.extend(
        _tier_line_items(
            "storage_iops",
            "Storage IOPS",
            "USD/IOPS-mo",
            rates.get("storage_iops_tiers", []),
        )
    )
    line_items.extend(
        _tier_line_items(
            "storage_throughput",
            "Storage throughput",
            "USD/MiBps-mo",
            rates.get("storage_throughput_tiers", []),
        )
    )
    for key, value in sorted(rates["fixed_hourly"].items()):
        line_items.append(
            {
                "key": key,
                "label": FIXED_RATE_LABELS.get(key, key.replace("_", " ").capitalize()),
                "value": value,
                "unit": "USD/hr",
                "note": "fixed, shared across tenants on the cluster",
            }
        )
    if not platform:
        return {"line_items": line_items}

    node_types = (
        db.session.query(
            CostNode.instance_type,
            func.avg(CostNode.cpu_capacity).label("cpu_capacity"),
            func.avg(CostNode.memory_gib).label("memory_gib"),
        )
        .filter(CostNode.instance_type.isnot(None), CostNode.last_seen >= start)
        .group_by(CostNode.instance_type)
        .all()
    )
    nodes: list[RatesNode] = [
        {
            "instance_type": row.instance_type,
            "hourly": node_hourly_price(
                row.instance_type,
                rates,
                cpu_capacity=row.cpu_capacity,
                memory_gib=row.memory_gib,
            ),
        }
        for row in node_types
    ]
    if not nodes:
        nodes = [
            {
                "instance_type": f"{family}.xlarge",
                "hourly": node_hourly_price(f"{family}.xlarge", rates),
            }
            for family in rates["node_family_xlarge_hourly"]
        ]
    nodes.sort(key=lambda node: node["instance_type"])

    return {"line_items": line_items, "nodes": nodes}


def _tier_line_items(
    key: str, label: str, unit: str, tiers: list[PriceTier]
) -> list[RateLineItem]:
    items: list[RateLineItem] = []
    floor = 0.0
    for tier in tiers:
        up_to = tier["up_to"]
        if tier["usd_month"]:
            span = (
                f"above {floor:,.0f}" if up_to is None else f"{floor:,.0f}–{up_to:,.0f}"
            )
            items.append(
                {
                    "key": key,
                    "label": label,
                    "value": tier["usd_month"],
                    "unit": unit,
                    "note": f"per volume, {span}",
                }
            )
        if up_to is not None:
            floor = up_to
    return items


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def _freshness(
    start: datetime.datetime,
    end: datetime.datetime,
    now: datetime.datetime,
    first_hour: datetime.datetime | None,
    platform: bool,
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

    result: FreshnessStats = {
        "collection_enabled": collection_enabled,
        "last_run_at": last_run.started_at.isoformat()
        if last_run is not None
        else None,
        "data_through": data_through.isoformat() if data_through is not None else None,
        "stale": stale,
    }
    # Counting/naming every missing or failed collection hour exposes
    # install-wide collector health, which tenant scopes shouldn't see --
    # and skips the per-hour query below entirely for them.
    if not platform:
        return result

    current_hour = now.replace(minute=0, second=0, microsecond=0)
    if now.minute < COLLECTION_GRACE_MINUTES:
        # The hour that just completed hasn't had its collection run yet.
        collection_cutoff = current_hour - datetime.timedelta(hours=1)
    else:
        collection_cutoff = current_hour
    range_end_hour = min(end, collection_cutoff)
    range_start_hour = start.replace(minute=0, second=0, microsecond=0)
    if range_start_hour < start:
        range_start_hour += datetime.timedelta(hours=1)
    if first_hour is not None and first_hour > range_start_hour:
        range_start_hour = first_hour
    expected_hours: list[datetime.datetime] = []
    cursor = range_start_hour
    while cursor < range_end_hour:
        expected_hours.append(cursor)
        cursor += datetime.timedelta(hours=1)

    # Only the columns needed here, not the rates/stats JSONB of every run.
    run_rows: list[Row[tuple[datetime.datetime, str, str | None]]] = (
        db.session.query(
            CostCollectionRun.period_start,
            CostCollectionRun.status,
            func.jsonb_extract_path_text(
                CostCollectionRun.stats, "last_attempt_status"
            ).label("last_attempt_status"),
        )
        .filter(
            CostCollectionRun.period_start >= range_start_hour,
            CostCollectionRun.period_start < range_end_hour,
        )
        .all()
    )
    runs_in_range = {row.period_start: row for row in run_rows}
    missing_hours = [hour for hour in expected_hours if hour not in runs_in_range]
    # A run's own `status` only describes the data it currently holds, which
    # may be older data preserved from a prior successful attempt (see
    # collector._record_attempt) -- `stats.last_attempt_status` reflects
    # what the most recent attempt for that hour actually did, so an hour
    # counts as failed here even if its held data (and `status`) still say
    # succeeded.
    failed_hours = sorted(
        hour
        for hour, row in runs_in_range.items()
        if row.status == "failed" or row.last_attempt_status == "failed"
    )
    result["missing_hours"] = len(missing_hours)
    result["failed_hours"] = [hour.isoformat() for hour in failed_hours]
    return result
