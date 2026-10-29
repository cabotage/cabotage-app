"""Daily cluster spend from AWS Cost Explorer, and the true-up against it.

Each refresh stores the trailing days' actuals and rewrites their true-up line
items (``aws_day`` set, no ``run_id``) so each day's line items sum to its
actuals. Amounts are amortized cost before credits, refunds and tax.
"""

import datetime
import json
import uuid
from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import boto3
import sqlalchemy as sa
from flask import current_app
from sqlalchemy import Row

from cabotage.server import db
from cabotage.server.applets.cost.constants import (
    ACTUALS_BUCKET_PATTERNS,
    ACTUALS_EGRESS_BUCKETS,
    ACTUALS_FIXED_BUCKETS,
    ACTUALS_LOOKBACK_DAYS,
    ACTUALS_NETWORK_BUCKETS,
    ACTUALS_OTHER_BUCKET,
    ACTUALS_SETTLED_DAYS,
    DEFAULT_ACTUALS_SERVICES,
    TRUE_UP_COMPONENT,
)
from cabotage.server.models.cost import CostAwsDaily, CostCollectionRun, CostLineItem

if TYPE_CHECKING:
    from mypy_boto3_ce.client import CostExplorerClient
    from mypy_boto3_ce.type_defs import (
        ExpressionTypeDef,
        GetCostAndUsageRequestTypeDef,
    )

CE_API_REGION = "us-east-1"

_DIRECT_CATEGORIES = ("compute", "backing_service", "build", "storage")
_NETWORK_CATEGORIES = ("network", "egress")

#: (hour, organization, project, environment, application, resource)
type _ScopeHour = tuple[
    datetime.datetime,
    uuid.UUID,
    uuid.UUID | None,
    uuid.UUID | None,
    uuid.UUID | None,
    uuid.UUID | None,
]


def bucket_for_usage_type(usage_type: str) -> str:
    for bucket, pattern in ACTUALS_BUCKET_PATTERNS:
        if pattern.search(usage_type):
            return bucket
    return ACTUALS_OTHER_BUCKET


def refresh_actuals(
    client: CostExplorerClient | None = None,
    today: datetime.date | None = None,
) -> list[datetime.date]:
    """Re-fetch and re-true-up the trailing settled days. Returns days with
    actuals."""
    config = current_app.config
    today = today or datetime.datetime.now(datetime.UTC).date()
    first_day = today - datetime.timedelta(days=ACTUALS_LOOKBACK_DAYS)
    # Recent days are still filling in; truing up to them would undercount.
    settled_end = today - datetime.timedelta(days=ACTUALS_SETTLED_DAYS - 1)
    if client is None:
        client = boto3.client("ce", region_name=CE_API_REGION)

    by_day = {
        day: buckets
        for day, buckets in fetch_daily_buckets(
            client, first_day, settled_end, _actuals_filter(config)
        ).items()
        if day < settled_end
    }

    fetched_at = datetime.datetime.now(datetime.UTC)
    CostAwsDaily.query.filter(
        CostAwsDaily.day >= first_day, CostAwsDaily.day < today
    ).delete()
    CostLineItem.query.filter(
        CostLineItem.aws_day >= first_day, CostLineItem.aws_day < today
    ).delete()
    for day, buckets in sorted(by_day.items()):
        for bucket, cost in sorted(buckets.items()):
            db.session.add(
                CostAwsDaily(
                    day=day, bucket=bucket, cost_usd=cost, fetched_at=fetched_at
                )
            )
    db.session.flush()

    days = sorted(by_day)
    for day in days:
        true_up_day(day, by_day[day])
    db.session.commit()
    return days


def fetch_daily_buckets(
    client: CostExplorerClient,
    first_day: datetime.date,
    end_day: datetime.date,
    cost_filter: ExpressionTypeDef,
) -> dict[datetime.date, dict[str, float]]:
    """Cost per day and bucket over ``[first_day, end_day)``."""
    by_day: defaultdict[datetime.date, defaultdict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    token: str | None = None
    while True:
        request: GetCostAndUsageRequestTypeDef = {
            "TimePeriod": {"Start": first_day.isoformat(), "End": end_day.isoformat()},
            "Granularity": "DAILY",
            "Metrics": ["AmortizedCost"],
            "Filter": cost_filter,
            "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        }
        if token:
            request["NextPageToken"] = token
        response = client.get_cost_and_usage(**request)
        for result in response.get("ResultsByTime", []):
            period = result.get("TimePeriod")
            if period is None:
                continue
            day = datetime.date.fromisoformat(period["Start"])
            for group in result.get("Groups", []):
                keys = group.get("Keys")
                metrics = group.get("Metrics")
                metric = metrics.get("AmortizedCost") if metrics else None
                amount = metric.get("Amount") if metric else None
                if keys and amount and float(amount):
                    by_day[day][bucket_for_usage_type(keys[0])] += float(amount)
        token = response.get("NextPageToken")
        if not token:
            break
    return {day: dict(buckets) for day, buckets in by_day.items() if buckets}


def _actuals_filter(config: Mapping[str, object]) -> ExpressionTypeDef:
    services = _config_list(config.get("COST_AWS_ACTUALS_SERVICES"))
    expressions: list[ExpressionTypeDef] = [
        {
            "Not": {
                "Dimensions": {
                    "Key": "RECORD_TYPE",
                    "Values": ["Credit", "Refund", "Tax"],
                }
            }
        },
        {"Dimensions": {"Key": "REGION", "Values": [str(config["COST_AWS_REGION"])]}},
        {
            "Dimensions": {
                "Key": "SERVICE",
                "Values": services or list(DEFAULT_ACTUALS_SERVICES),
            }
        },
    ]
    account_id = config.get("COST_AWS_ACCOUNT_ID")
    if account_id:
        expressions.append(
            {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [str(account_id)]}}
        )
    tag = config.get("COST_AWS_ACTUALS_TAG")
    if tag:
        key, sep, value = str(tag).partition("=")
        if not sep or not key or not value:
            raise ValueError(f"COST_AWS_ACTUALS_TAG must be KEY=VALUE, got {tag!r}")
        expressions.append({"Tags": {"Key": key, "Values": [value]}})
    return {"And": expressions}


def _config_list(value: object) -> list[str]:
    # MetaFlaskEnv leaves an env-set JSON array as a str.
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError(
                f"COST_AWS_ACTUALS_SERVICES is not a JSON array: {exc}"
            ) from exc
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TypeError("COST_AWS_ACTUALS_SERVICES must be a list of service names")
    return cast(list[str], value)


def true_up_day(day: datetime.date, buckets: Mapping[str, float]) -> None:
    """Rewrite ``day``'s true-ups so its line items sum to its actuals, scaled
    to the hours that were collected."""
    CostLineItem.query.filter(CostLineItem.aws_day == day).delete()

    day_start = datetime.datetime.combine(day, datetime.time(), datetime.UTC)
    day_end = day_start + datetime.timedelta(days=1)
    hours: int = CostCollectionRun.query.filter(
        CostCollectionRun.status == "succeeded",
        CostCollectionRun.period_start >= day_start,
        CostCollectionRun.period_start < day_end,
    ).count()
    if not hours:
        return
    scale = hours / 24
    rows: list[
        Row[
            tuple[
                datetime.datetime,
                uuid.UUID,
                uuid.UUID | None,
                uuid.UUID | None,
                uuid.UUID | None,
                uuid.UUID | None,
                str,
                float | None,
                Decimal | None,
            ]
        ]
    ] = (
        db.session.query(
            CostLineItem.period_start,
            CostLineItem.organization_id,
            CostLineItem.project_id,
            CostLineItem.environment_id,
            CostLineItem.application_id,
            CostLineItem.resource_id,
            CostLineItem.category,
            sa.func.sum(CostLineItem.quantity).label("quantity"),
            sa.func.sum(CostLineItem.cost_usd).label("cost"),
        )
        .filter(
            CostLineItem.period_start >= day_start,
            CostLineItem.period_start < day_end,
            CostLineItem.run_id.isnot(None),
        )
        .group_by(
            CostLineItem.period_start,
            CostLineItem.organization_id,
            CostLineItem.project_id,
            CostLineItem.environment_id,
            CostLineItem.application_id,
            CostLineItem.resource_id,
            CostLineItem.category,
        )
        .all()
    )

    estimated_network = 0.0
    estimated_other = 0.0
    network_weights: defaultdict[_ScopeHour, float] = defaultdict(float)
    egress_weights: defaultdict[_ScopeHour, float] = defaultdict(float)
    direct_weights: defaultdict[_ScopeHour, float] = defaultdict(float)
    for (
        period_start,
        org_id,
        project_id,
        env_id,
        app_id,
        resource_id,
        category,
        quantity,
        total_cost,
    ) in rows:
        key: _ScopeHour = (
            period_start,
            org_id,
            project_id,
            env_id,
            app_id,
            resource_id,
        )
        cost = float(total_cost or 0.0)
        if category in _NETWORK_CATEGORIES:
            estimated_network += cost
            weights = network_weights if category == "network" else egress_weights
            weights[key] += float(quantity or 0.0)
        else:
            estimated_other += cost
            if category in _DIRECT_CATEGORIES:
                direct_weights[key] += cost

    actual_network = scale * sum(buckets.get(b, 0.0) for b in ACTUALS_NETWORK_BUCKETS)
    actual_other = scale * sum(
        cost for b, cost in buckets.items() if b not in ACTUALS_NETWORK_BUCKETS
    )
    _spread(
        day,
        "network",
        actual_network - estimated_network,
        network_weights if _total(network_weights) > 0 else egress_weights,
    )
    _spread(day, "overhead", actual_other - estimated_other, direct_weights)


def _total(weights: Mapping[_ScopeHour, float]) -> float:
    return sum(w for w in weights.values() if w > 0)


def _spread(
    day: datetime.date,
    category: str,
    delta: float,
    weights: Mapping[_ScopeHour, float],
) -> None:
    total = _total(weights)
    if total <= 0 or abs(delta) < 1e-6:
        return
    for key, weight in weights.items():
        if weight <= 0:
            continue
        period_start, org_id, project_id, env_id, app_id, resource_id = key
        cost = delta * weight / total
        db.session.add(
            CostLineItem(
                run_id=None,
                aws_day=day,
                period_start=period_start,
                organization_id=org_id,
                project_id=project_id,
                environment_id=env_id,
                application_id=app_id,
                resource_id=resource_id,
                category=category,
                meter="shared",
                component=TRUE_UP_COMPONENT,
                quantity=cost,
                unit="USD",
                usage_quantity=None,
                request_quantity=None,
                cost_usd=cost,
            )
        )


def actuals_rate_overrides(today: datetime.date | None = None) -> dict[str, object]:
    """``network_gb``, ``egress_gb`` and ``fixed_hourly`` from the latest
    settled day of actuals that has collected hours. Each per-GB rate spreads
    its AWS charge over the tenant bytes we meter, as the true-up does."""
    today = today or datetime.datetime.now(datetime.UTC).date()
    latest = (
        db.session.query(sa.func.max(CostAwsDaily.day))
        .join(
            CostCollectionRun,
            sa.and_(
                CostCollectionRun.status == "succeeded",
                sa.func.date(sa.func.timezone("UTC", CostCollectionRun.period_start))
                == CostAwsDaily.day,
            ),
        )
        .filter(
            CostAwsDaily.day <= today - datetime.timedelta(days=ACTUALS_SETTLED_DAYS)
        )
        .scalar()
    )
    if latest is None:
        return {}
    daily: list[CostAwsDaily] = CostAwsDaily.query.filter_by(day=latest).all()
    buckets = {row.bucket: float(row.cost_usd) for row in daily}
    day_start = datetime.datetime.combine(latest, datetime.time(), datetime.UTC)
    day_end = day_start + datetime.timedelta(days=1)

    # Each category is scaled to a day by the hours it was actually metered,
    # so hours where e.g. Traefik returned nothing don't inflate its rate.
    overrides: dict[str, object] = {}
    per_gb = (
        ("network_gb", "network", buckets.get("cross_az", 0.0)),
        (
            "egress_gb",
            "egress",
            sum(buckets.get(b, 0.0) for b in ACTUALS_EGRESS_BUCKETS),
        ),
    )
    for rate_key, category, cost in per_gb:
        gb, _, hours = _metered(category, day_start, day_end)
        if gb > 0 and cost > 0:
            overrides[rate_key] = cost / (gb * 24 / hours)

    _, storage_priced, storage_hours = _metered("storage", day_start, day_end)
    # Scale to a full day so a partly collected day isn't all "unattributed".
    storage_priced_day = storage_priced * 24 / storage_hours if storage_hours else 0.0
    fixed_hourly = {
        bucket: buckets[bucket] / 24
        for bucket in ACTUALS_FIXED_BUCKETS
        if buckets.get(bucket, 0.0) > 0
    }
    unattributed_storage = buckets.get("storage", 0.0) - storage_priced_day
    if unattributed_storage > 0:
        fixed_hourly["unattributed_storage"] = unattributed_storage / 24
    overrides["fixed_hourly"] = fixed_hourly
    return overrides


def _metered(
    category: str, start: datetime.datetime, end: datetime.datetime
) -> tuple[float, float, int]:
    """Quantity, cost and hours metered for ``category`` in ``[start, end)``."""
    quantity, cost, hours = (
        db.session.query(
            sa.func.coalesce(sa.func.sum(CostLineItem.quantity), 0),
            sa.func.coalesce(sa.func.sum(CostLineItem.cost_usd), 0),
            sa.func.count(sa.distinct(CostLineItem.period_start)),
        )
        .filter(
            CostLineItem.period_start >= start,
            CostLineItem.period_start < end,
            CostLineItem.run_id.isnot(None),
            CostLineItem.category == category,
        )
        .one()
    )
    return float(quantity), float(cost), int(hours)
