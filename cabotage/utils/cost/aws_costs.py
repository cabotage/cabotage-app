"""Static AWS on-demand cost rates used to estimate workload spend."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CostRate:
    rate_usd: float
    unit_label: str
    icon: str
    label: str


RATES: dict[str, CostRate] = {
    "vcpu_hours": CostRate(0.020, "hr", "cpu", "vCPU Hours"),
    "ram_gb_hours": CostRate(0.010, "GB-hr", "hard-drive", "RAM"),
    "egress_gb": CostRate(0.045, "GB", "upload", "Egress"),
    "build_minutes": CostRate(0.004, "min", "tool", "Build Minutes"),
    "block_storage_gb": CostRate(0.12, "GB-mo", "hard-drive", "Block Storage"),
    "object_storage_gb": CostRate(0.03, "GB-mo", "hard-drive", "Object Storage"),
    "db_storage_gb": CostRate(0.15, "GB-mo", "layers", "DB Storage"),
    "postgres_gb_hours": CostRate(0.015, "GB-hr", "layers", "PostgreSQL"),
    "redis_gb_hours": CostRate(0.015, "GB-hr", "layers", "Redis"),
}


def cost_for(meter_key: str, value: float) -> float:
    rate = RATES.get(meter_key)
    if rate is None or value <= 0:
        return 0.0
    return value * rate.rate_usd
