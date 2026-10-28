"""Cost applet types."""

import datetime
import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import Literal, NotRequired, TypedDict

from sqlalchemy.orm import InstrumentedAttribute

# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------


class PriceTier(TypedDict):
    """USD per unit-month for the slice of an amount up to ``up_to``.

    Tiers are ordered; ``up_to`` of ``None`` means no upper bound.
    """

    up_to: float | None
    usd_month: float


class StorageVolume(TypedDict):
    """How backing-service EBS volumes are provisioned (from their StorageClass)."""

    volume_type: str
    iops: float | None
    iops_per_gib: float | None
    throughput_mibps: float | None


class RatesSource(TypedDict):
    basis: str
    region: str
    checked: str


class CostRates(TypedDict):
    """The effective, JSON-serializable rate table shape, as persisted on
    every ``CostCollectionRun.rates`` and returned by :func:`effective_rates`.
    """

    vcpu_hour: float
    memory_gib_hour: float
    storage_gib_month: float
    egress_gb: float
    fixed_hourly: dict[str, float]
    node_family_xlarge_hourly: dict[str, float]
    node_size_factor: dict[str, float]
    source: NotRequired[RatesSource]
    node_instance_hourly: NotRequired[dict[str, float]]
    storage_volume: NotRequired[StorageVolume]
    storage_iops_tiers: NotRequired[list[PriceTier]]
    storage_throughput_tiers: NotRequired[list[PriceTier]]


class SnapshotRates(TypedDict, total=False):
    """The subset of ``CostRates`` refreshed from the AWS Price List."""

    source: RatesSource
    node_instance_hourly: dict[str, float]
    storage_gib_month: float
    storage_volume: StorageVolume
    storage_iops_tiers: list[PriceTier]
    storage_throughput_tiers: list[PriceTier]


# ---------------------------------------------------------------------------
# AWS Price List
# ---------------------------------------------------------------------------


class PriceListDimension(TypedDict):
    unit: str
    pricePerUnit: dict[str, str]


class PriceListTerm(TypedDict):
    priceDimensions: dict[str, PriceListDimension]


class PriceListTerms(TypedDict):
    OnDemand: NotRequired[dict[str, PriceListTerm]]


class PriceListProduct(TypedDict):
    productFamily: str
    sku: str
    attributes: dict[str, str]


class PriceListItem(TypedDict):
    product: PriceListProduct
    terms: PriceListTerms


class VolumeRates(TypedDict):
    storage_gib_month: float
    storage_iops_tiers: list[PriceTier]
    storage_throughput_tiers: list[PriceTier]


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


type PodKey = tuple[str | None, str | None]


type ScopeKey = tuple[
    uuid.UUID | None,
    uuid.UUID | None,
    uuid.UUID | None,
    uuid.UUID | None,
    uuid.UUID | None,
]


type GroupKey = tuple[
    str,
    uuid.UUID | None,
    uuid.UUID | None,
    uuid.UUID | None,
    uuid.UUID | None,
    uuid.UUID | None,
    str,
]


class PodClassification(TypedDict):
    category: str
    organization_id: uuid.UUID
    project_id: uuid.UUID | None
    environment_id: uuid.UUID | None
    application_id: uuid.UUID | None
    resource_id: uuid.UUID | None
    component: str


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


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


class MoverRow(TypedDict):
    key: str
    name: str
    context: str | None
    cost: float
    change: float


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
    """One node over the report range. CPU/memory figures are hour-weighted
    averages (vCPU, GiB); dollar figures are None when the node was never
    priced (unknown instance type or capacity)."""

    name: str
    instance_type: str | None
    capacity_type: str | None
    cpu_capacity: float | None
    memory_gib: float | None
    node_hours: float
    cpu_requested: float
    cpu_used: float
    memory_requested: float
    memory_used: float
    cost: float | None
    requested_cost: float | None
    used_cost: float | None
    idle_cost: float | None


class NodeInventoryStats(TypedDict):
    """Node pricing health as of the latest collected hour."""

    period_start: str | None
    error: str | None
    nodes_unpriced: int


class UnallocatedStats(TypedDict):
    pods_seen: float
    pods_unallocated: float
    share: float | None


class PlatformBlock(TypedDict):
    reconciliation: ReconciliationStats
    capacity_series: CapacitySeries
    nodes: list[NodeRow]
    node_inventory: NodeInventoryStats
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
    #: Cluster node instance types — platform scope only; a tenant report
    #: would leak install-wide infrastructure detail.
    nodes: NotRequired[list[RatesNode]]


class FreshnessStats(TypedDict):
    collection_enabled: bool
    last_run_at: str | None
    data_through: str | None
    stale: bool
    #: Platform scope only — counting/naming every missing or failed
    #: collection hour exposes install-wide collector health to tenants.
    missing_hours: NotRequired[int]
    failed_hours: NotRequired[list[str]]


#: A breakdown's raw group-by key: a scope id, or a component/process name.
type BreakdownKey = uuid.UUID | str


#: What a breakdown's name-resolver callback returns for one raw group-by
#: key: (display name, context line, link slugs, soft-deleted?). A key
#: missing from the returned dict (no current resolver omits one, but
#: ``_build_rows`` handles it defensively) is skipped.
type ResolvedName = tuple[str, str | None, BreakdownSlugs, bool]


#: Resolves every raw group-by key for a breakdown in one batch (one
#: ``Model.query.filter(Model.id.in_(keys))`` call) rather than one query
#: per key.
type NameResolver = Callable[[list[BreakdownKey]], dict[BreakdownKey, ResolvedName]]


#: (aggregation column, optional category filter) per breakdown name.
type BreakdownDimension = tuple[
    InstrumentedAttribute[BreakdownKey | None], list[str] | None
]


#: The 5-tuple ``_aggregate_breakdown`` hands to ``_build_rows``.
type BreakdownAggregates = tuple[
    defaultdict[BreakdownKey, float],
    defaultdict[BreakdownKey, defaultdict[str, float]],
    dict[BreakdownKey, float],
    defaultdict[BreakdownKey, float],
    defaultdict[BreakdownKey, float],
]


class RangeConfig(TypedDict):
    duration: datetime.timedelta
    interval: Interval
    label: str


class HeatmapPayload(TypedDict):
    days: list[str]
    hours: list[int]
    values: list[list[float]]


# ---------------------------------------------------------------------------
# Demo seed
# ---------------------------------------------------------------------------


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
    gone_after: datetime.datetime | None


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
