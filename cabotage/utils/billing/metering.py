"""Utils to determine usage for billing.

Each collect_* function returns one meter's usage for an org over the last hour.
"""
from typing import Callable

from cabotage.server.models.auth import Organization


def _parse_cpu(cpu_str: str) -> float:
    """Convert K8s CPU string to vCPU count. '250m' -> 0.25, '1' -> 1.0."""
    if cpu_str.endswith("m"):
        return int(cpu_str[:-1]) / 1000
    return float(cpu_str)


def _parse_memory_gi(mem_str: str) -> float:
    """Convert K8s memory string to GB. '512Mi' -> 0.5, '1Gi' -> 1.0."""
    if mem_str.endswith("Gi"):
        return float(mem_str[:-2])
    if mem_str.endswith("Mi"):
        return float(mem_str[:-2]) / 1024
    return 0.0


def collect_vcpu_hours(org: Organization) -> float:
    """Sum vCPU requests x replicas across all active apps."""
    return 0.0


def collect_ram_gb_hours(org: Organization) -> float:
    """Sum RAM requests x replicas across all active apps."""
    return 0.0


def collect_egress_gb(org: Organization) -> float:
    """Outbound data transfer. Needs K8s network metrics or cloud flow logs."""
    return 0.0


def collect_block_storage_gb(org: Organization) -> float:
    """PVC-backed persistent volume usage. Needs K8s API queries."""
    return 0.0


def collect_object_storage_gb(org: Organization) -> float:
    """S3-compatible object storage usage."""
    return 0.0


def collect_db_storage_gb(org: Organization) -> float:
    """Managed database storage. Needs pg_database_size queries."""
    return 0.0


def collect_postgres_gb_hours(org: Organization) -> float:
    """Managed PostgreSQL compute hours."""
    return 0.0


def collect_redis_gb_hours(org: Organization) -> float:
    """Managed Redis compute hours."""
    return 0.0


def collect_build_minutes(org: Organization) -> float:
    """CI/CD build time from completed builds in the last hour."""
    return 0.0


def collect_tailscale_nodes(org: Organization) -> float:
    """Tailscale private networking node count."""
    return 0.0

COLLECTORS: dict[str, Callable[[Organization], float]] = {
    "vcpu_hours": collect_vcpu_hours,
    "ram_gb_hours": collect_ram_gb_hours,
    "egress_gb": collect_egress_gb,
    "block_storage_gb": collect_block_storage_gb,
    "object_storage_gb": collect_object_storage_gb,
    "db_storage_gb": collect_db_storage_gb,
    "postgres_gb_hours": collect_postgres_gb_hours,
    "redis_gb_hours": collect_redis_gb_hours,
    "build_minutes": collect_build_minutes,
    "tailscale_nodes": collect_tailscale_nodes,
}

def collect_usage(org: Organization) -> dict[str, float]:
    """Collect all metered usage for an org. Keys match METERS dict."""
    return {k: fn(org) for k, fn in COLLECTORS.items()}

