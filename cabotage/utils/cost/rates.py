"""AWS us-east-2 on-demand list-price rate table for cost estimation.

Prices checked 2026-10-11. Any key may be overridden via the ``COST_RATES``
app config (a dict, or a JSON object string when set via the environment;
nested dicts are merged key-by-key, not replaced wholesale). The effective
rates are recorded on every collection run so the UI can render a "how costs
are calculated" panel.
"""

import copy
import json
from collections.abc import Mapping
from typing import TypedDict, cast


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


DEFAULT_RATES: CostRates = {
    # Per-vCPU / per-GiB-memory compute, derived from m8g.xlarge ($0.17952,
    # 4 vCPU / 16 GiB) and c8g.xlarge ($0.15952, 4 vCPU / 8 GiB).
    "vcpu_hour": 0.03488,
    "memory_gib_hour": 0.0025,
    # gp3 EBS storage.
    "storage_gib_month": 0.08,
    # Internet egress.
    "egress_gb": 0.09,
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
}


def effective_rates(config: Mapping[str, object] | None = None) -> CostRates:
    """Return :data:`DEFAULT_RATES` merged with any ``COST_RATES`` overrides.

    ``config`` is a Flask config mapping; defaults to ``current_app.config``.
    The result is a plain, JSON-serializable dict safe to persist on a
    ``CostCollectionRun``.
    """
    rates: dict[str, object] = dict(copy.deepcopy(DEFAULT_RATES))
    if config is None:
        from flask import current_app

        config = current_app.config
    overrides_raw = config.get("COST_RATES")
    # Set via CABOTAGE_COST_RATES, MetaFlaskEnv leaves a JSON object as a str.
    if isinstance(overrides_raw, str):
        try:
            overrides_raw = json.loads(overrides_raw)
        except ValueError as exc:
            raise ValueError(f"COST_RATES is not valid JSON: {exc}") from exc
    if overrides_raw is None:
        return cast(CostRates, rates)
    if not isinstance(overrides_raw, dict):
        raise TypeError(
            f"COST_RATES must be a JSON object, got {type(overrides_raw).__name__}"
        )
    overrides: dict[str, object] = overrides_raw
    for key, value in overrides.items():
        existing = rates.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            rates[key] = {**existing, **value}
        else:
            rates[key] = value
    return cast(CostRates, rates)


def node_hourly_price(
    instance_type: str | None,
    rates: CostRates,
    *,
    cpu_capacity: float | None = None,
    memory_gib: float | None = None,
) -> float:
    """Hourly on-demand price for a node instance type, from ``rates``.

    Falls back to pricing by vCPU/memory capacity at the standard compute
    rates when the instance type's family/size isn't recognized.
    """
    family, _, size = (instance_type or "").partition(".")
    xlarge_price = rates["node_family_xlarge_hourly"].get(family)
    factor = rates["node_size_factor"].get(size)
    if xlarge_price is not None and factor is not None:
        return xlarge_price * factor
    cpu = cpu_capacity or 0.0
    mem = memory_gib or 0.0
    return cpu * rates["vcpu_hour"] + mem * rates["memory_gib_hour"]
