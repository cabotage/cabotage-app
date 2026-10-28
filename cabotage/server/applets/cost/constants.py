"""Cost applet constants."""

import datetime
import re

from cabotage.server.applets.cost.types import CostRates, RangeConfig

# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------

#: gp3 volumes include this much IOPS and throughput at no extra charge, and
#: are provisioned with exactly that much unless the StorageClass asks for more.
GP3_BASELINE_IOPS = 3000.0
GP3_BASELINE_THROUGHPUT_MIBPS = 125.0

DEFAULT_RATES: CostRates = {
    # Per-vCPU / per-GiB-memory compute, derived from m8g.xlarge ($0.17952,
    # 4 vCPU / 16 GiB) and c8g.xlarge ($0.15952, 4 vCPU / 8 GiB).
    "vcpu_hour": 0.03488,
    "memory_gib_hour": 0.0025,
    # gp3 EBS storage.
    "storage_gib_month": 0.08,
    # Internet egress.
    "egress_gb": 0.09,
    # Pod traffic, sent + received. ~2/3 crosses AZs at $0.01/GB per side.
    "network_gb": 0.0067,
    # Fixed hourly costs shared across all tenants on the cluster.
    "fixed_hourly": {
        "eks_control_plane": 0.10,
        "nat_gateway": 0.045,
        "load_balancer": 0.0225,
    },
    # On-demand hourly price for each Graviton family at the ".xlarge" size;
    # other sizes are derived via node_size_factor.
    "node_family_xlarge_hourly": {
        "m8g": 0.17952,
        "c8g": 0.15952,
        "r8g": 0.23562,
        "t4g": 0.1344,
    },
    "node_size_factor": {
        "nano": 0.03125,
        "micro": 0.0625,
        "small": 0.125,
        "medium": 0.25,
        "large": 0.5,
        "xlarge": 1.0,
        "2xlarge": 2.0,
        "4xlarge": 4.0,
        "8xlarge": 8.0,
        "12xlarge": 12.0,
        "16xlarge": 16.0,
        "24xlarge": 24.0,
    },
    "source": {
        "basis": "AWS on-demand list price",
        "region": "us-east-2",
        "checked": "2026-10-11",
    },
}


# ---------------------------------------------------------------------------
# AWS Price List
# ---------------------------------------------------------------------------

NODE_LOOKBACK = datetime.timedelta(days=7)

EC2_INSTANCE_FILTERS = {
    "operatingSystem": "Linux",
    "tenancy": "Shared",
    "preInstalledSw": "NA",
    "capacitystatus": "Used",
    "marketoption": "OnDemand",
}

IO2_IOPS_TIERS: list[tuple[str, float | None]] = [
    ("EBS IOPS", 32000.0),
    ("EBS IOPS Tier 2", 64000.0),
    ("EBS IOPS Tier 3", None),
]

STORAGE_UNITS = {"GB-Mo", "GB-month"}

# Throughput is listed per GiBps-month even though it's sold per MiBps.
THROUGHPUT_UNIT_MIBPS = {"GiBps-mo": 1024.0, "MiBps-mo": 1.0}

DEFAULT_CLASS_ANNOTATION = "storageclass.kubernetes.io/is-default-class"

DEFAULT_VOLUME_TYPE = {"ebs.csi.aws.com": "gp3", "kubernetes.io/aws-ebs": "gp2"}


# ---------------------------------------------------------------------------
# AWS actuals (Cost Explorer)
# ---------------------------------------------------------------------------

#: Cost Explorer revises recent days, so each refresh re-fetches this many.
ACTUALS_LOOKBACK_DAYS = 7

#: Services counted as the cluster's bill. Excludes RDS, OpenSearch, etc.
DEFAULT_ACTUALS_SERVICES = [
    "Amazon Elastic Compute Cloud - Compute",
    "EC2 - Other",
    "Amazon Elastic Load Balancing",
    "Amazon Virtual Private Cloud",
    "Amazon Elastic Container Service for Kubernetes",
]

#: Usage type -> bucket, first match wins.
ACTUALS_BUCKET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("compute", re.compile(r"BoxUsage|SpotUsage|DedicatedUsage|HostUsage|CPUCredits")),
    ("storage", re.compile(r"EBS:")),
    ("cross_az", re.compile(r"DataTransfer-Regional-Bytes")),
    ("inter_region", re.compile(r"-AWS-(In|Out)-Bytes")),
    ("internet_egress", re.compile(r"DataTransfer-Out-Bytes")),
    ("load_balancer", re.compile(r"LoadBalancerUsage|LCUUsage|DataProcessing-Bytes")),
    ("public_ipv4", re.compile(r"PublicIPv4")),
    ("nat_gateway", re.compile(r"NatGateway")),
    ("eks_control_plane", re.compile(r"EKS-Hours")),
]
ACTUALS_OTHER_BUCKET = "other"

ACTUALS_NETWORK_BUCKETS = ("cross_az", "inter_region", "internet_egress")

#: Become ``fixed_hourly``. Storage is handled separately.
ACTUALS_FIXED_BUCKETS = (
    "load_balancer",
    "public_ipv4",
    "nat_gateway",
    "eks_control_plane",
    ACTUALS_OTHER_BUCKET,
)

ACTUALS_BUCKET_LABELS = {
    "compute": "EC2 instances",
    "storage": "EBS volumes",
    "cross_az": "Cross-AZ transfer",
    "inter_region": "Inter-region transfer",
    "internet_egress": "Internet egress",
    "load_balancer": "Load balancers",
    "public_ipv4": "Public IPv4",
    "nat_gateway": "NAT gateway",
    "eks_control_plane": "EKS control plane",
    ACTUALS_OTHER_BUCKET: "Other",
}

TRUE_UP_COMPONENT = "aws-true-up"


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

GIB = 1024.0**3

STEP_SECONDS = 300
STEP_HOURS = STEP_SECONDS / 3600.0
STEPS_PER_HOUR = 12

# EBS bills per GiB-month, so each hour is recorded as GiB-mo (1/730 of a
# month); summed over a month the quantity reads as provisioned GiB.
HOURS_PER_MONTH = 730.0

# CostLineItem.component is String(64); resource slugs are unbounded.
COMPONENT_MAX_LEN = 64

BUILD_POD_RE = re.compile(r"^(?:image|release|omnibus)build-([0-9a-f]{8})-")

# Every pod query also keeps `node` (native on kube-state-metrics; Alloy adds
# it to cAdvisor) so per-pod usage can be rolled up per node.
CPU_USAGE_QUERY = (
    "sum by (namespace, pod, node) (rate(container_cpu_usage_seconds_total"
    '{container!="",container!="POD"}[5m]))'
)

MEM_USAGE_QUERY = (
    "sum by (namespace, pod, node) (container_memory_working_set_bytes"
    '{container!="",container!="POD"})'
)

CPU_REQUEST_QUERY = (
    "sum by (namespace, pod, node) "
    '(kube_pod_container_resource_requests{resource="cpu"})'
)

MEM_REQUEST_QUERY = (
    "sum by (namespace, pod, node) "
    '(kube_pod_container_resource_requests{resource="memory"})'
)

# Range lookback at period_end so pods that terminated mid-hour (builds,
# replaced replicas) still have labels to attribute their usage samples.
POD_LABELS_QUERY = "last_over_time(kube_pod_labels[1h])"

TRAEFIK_QUERY = "sum by (service) (increase(traefik_service_responses_bytes_total[1h]))"

# eth0 excludes hostNetwork pods.
NETWORK_RECEIVE_QUERY = (
    "sum by (namespace, pod) "
    '(increase(container_network_receive_bytes_total{interface="eth0"}[1h]))'
)

NETWORK_TRANSMIT_QUERY = (
    "sum by (namespace, pod) "
    '(increase(container_network_transmit_bytes_total{interface="eth0"}[1h]))'
)

MEM_SUFFIXES: dict[str, float] = {
    "Ki": 1024.0,
    "Mi": 1024.0**2,
    "Gi": 1024.0**3,
    "Ti": 1024.0**4,
    "K": 1000.0,
    "M": 1000.0**2,
    "G": 1000.0**3,
    "T": 1000.0**4,
}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

CATEGORY_ORDER = [
    "compute",
    "backing_service",
    "storage",
    "egress",
    "network",
    "build",
    "overhead",
]

CATEGORY_LABELS = {
    "compute": "Compute",
    "backing_service": "Backing services",
    "storage": "Storage",
    "egress": "Egress",
    "network": "Network",
    "build": "Builds",
    "overhead": "Shared overhead",
}

METER_ORDER = ["vcpu", "memory", "storage", "egress", "network", "shared"]

METER_LABELS = {
    "vcpu": "vCPU",
    "memory": "Memory",
    "storage": "Storage",
    "egress": "Egress",
    "network": "Network",
    "shared": "Shared",
}

METER_UNITS = {
    "vcpu": "vCPU-hr",
    "memory": "GiB-hr",
    "storage": "GiB-mo",
    "egress": "GB",
    "network": "GB",
    "shared": "USD",
}

METER_RATE_KEY = {
    "vcpu": "vcpu_hour",
    "memory": "memory_gib_hour",
    "storage": "storage_gib_month",
    "egress": "egress_gb",
    "network": "network_gb",
}

# "24h" and "7d" chart hourly (matching the collector's native grain); "30d"
# and "90d" chart daily to keep the series readable. "mtd" is handled
# separately since its length changes through the month.
RANGE_CONFIG: dict[str, RangeConfig] = {
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

# collect_costs backfills every non-succeeded hour in its window on each
# beat tick; without this, a persistently-failing hour (e.g. a sustained
# Mimir outage) gets hammered every tick instead of backing off.
FAILED_HOUR_RETRY_AFTER = datetime.timedelta(hours=6)

# A custom start/end range charts hourly up to this span, daily beyond it.
CUSTOM_HOURLY_SPAN = datetime.timedelta(days=3)

# Extent series are capped at roughly this many buckets.
MAX_EXTENT_BUCKETS = 120

CALENDAR_SPAN = datetime.timedelta(days=90)

TOP_N_BREAKDOWN_SERIES = 7

RATE_LINE_ITEMS: list[tuple[str, str, str, str, str | None]] = [
    ("vcpu", "vCPU", "vcpu_hour", "USD/vCPU-hr", None),
    ("memory", "Memory", "memory_gib_hour", "USD/GiB-hr", None),
    ("storage", "Storage", "storage_gib_month", "USD/GiB-mo", None),
    ("egress", "Egress", "egress_gb", "USD/GB", None),
    (
        "network",
        "Network",
        "network_gb",
        "USD/GB",
        "pod bytes sent + received; covers cross-AZ transfer",
    ),
]

FIXED_RATE_LABELS: dict[str, str] = {
    "eks_control_plane": "EKS control plane",
    "nat_gateway": "NAT gateway",
    "load_balancer": "Load balancer",
    "public_ipv4": "Public IPv4",
    "unattributed_storage": "Unattributed EBS volumes",
    "other": "Other AWS",
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


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

DEFAULT_RANGE = "mtd"
RANGE_KEYS = {DEFAULT_RANGE, *RANGE_CONFIG}
CUSTOM_RANGE_FLOOR = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)

# Rejecting spans longer than this keeps a custom ?start=&end= from forcing
# _freshness/bucketing to iterate decades of hours.
CUSTOM_RANGE_MAX_SPAN = datetime.timedelta(days=366)


# ---------------------------------------------------------------------------
# Demo seed
# ---------------------------------------------------------------------------

RUN_COLUMNS = (
    "id",
    "period_start",
    "status",
    "started_at",
    "finished_at",
    "error",
    "rates",
    "stats",
)

LINE_ITEM_COLUMNS = (
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

# Candidate hours for a build to kick off, and the per-(app, hour) chance of
# one happening — tuned so an active app sees roughly 3-5 builds/week.
DEMO_BUILD_HOURS = (9, 10, 11, 13, 14, 16, 20)
DEMO_BUILD_HOUR_PROBABILITY = 0.15
DEMO_BUILD_CPU_REQUEST = 4.0  # vCPU, a realistic buildkit job size
DEMO_BUILD_MEM_REQUEST = 6.0  # GiB
