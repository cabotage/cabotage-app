"""AWS us-east-2 on-demand list-price rate table for cost estimation.

Prices checked 2026-10-11. When ``COST_AWS_PRICING_ENABLED`` is set, the
latest :class:`~cabotage.server.models.cost.CostPriceSnapshot` (refreshed from
the AWS Price List API) is layered on top, then rates derived from Cost
Explorer actuals when ``COST_AWS_ACTUALS_ENABLED``. Any key may then be
overridden via the ``COST_RATES`` app config (a dict, or a JSON object string
when set via the environment; nested dicts are merged key-by-key, not replaced
wholesale). The effective rates are recorded on every collection run so the UI
can render a "how costs are calculated" panel.
"""

import copy
import json
from collections.abc import Mapping, Sequence
from typing import cast

from cabotage.server.applets.cost.aws_actuals import actuals_rate_overrides
from cabotage.server.applets.cost.constants import (
    DEFAULT_RATES,
    GP3_BASELINE_IOPS,
    GP3_BASELINE_THROUGHPUT_MIBPS,
)
from cabotage.server.applets.cost.types import CostRates, PriceTier
from cabotage.server.models.cost import CostPriceSnapshot


def effective_rates(config: Mapping[str, object] | None = None) -> CostRates:
    """Return :data:`DEFAULT_RATES`, then the latest AWS price snapshot (when
    ``COST_AWS_PRICING_ENABLED``), then Cost Explorer actuals (when
    ``COST_AWS_ACTUALS_ENABLED``), then any ``COST_RATES`` overrides.

    ``config`` is a Flask config mapping; defaults to ``current_app.config``.
    The result is a plain, JSON-serializable dict safe to persist on a
    ``CostCollectionRun``.
    """
    rates: dict[str, object] = dict(copy.deepcopy(DEFAULT_RATES))
    if config is None:
        from flask import current_app

        config = current_app.config
    if config.get("COST_AWS_PRICING_ENABLED"):
        snapshot = CostPriceSnapshot.latest()
        if snapshot is not None:
            _merge_into(rates, dict(snapshot.rates))
    if config.get("COST_AWS_ACTUALS_ENABLED"):
        # Replace, don't merge: drops default fixed costs the cluster lacks.
        rates.update(actuals_rate_overrides())
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
    overrides = cast(dict[str, object], overrides_raw)
    _validate_overrides(overrides)
    family_overrides = overrides.get("node_family_xlarge_hourly")
    if isinstance(family_overrides, dict) and "node_instance_hourly" in rates:
        snapshot_prices = cast(dict[str, float], rates["node_instance_hourly"])
        # A family override beats the snapshot's list price for that family.
        rates["node_instance_hourly"] = {
            instance_type: price
            for instance_type, price in snapshot_prices.items()
            if instance_type.partition(".")[0] not in family_overrides
        }
    _merge_into(rates, overrides)
    return cast(CostRates, rates)


_RATE_KEYS = CostRates.__required_keys__ | CostRates.__optional_keys__
_SCALAR_RATE_KEYS = {
    "vcpu_hour",
    "memory_gib_hour",
    "storage_gib_month",
    "egress_gb",
    "network_gb",
}
_PRICE_MAP_KEYS = {
    "fixed_hourly",
    "node_family_xlarge_hourly",
    "node_size_factor",
    "node_instance_hourly",
}


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _validate_overrides(overrides: Mapping[str, object]) -> None:
    for key, value in overrides.items():
        if key not in _RATE_KEYS:
            raise ValueError(f"COST_RATES has unknown key {key!r}")
        if key in _SCALAR_RATE_KEYS and not _is_number(value):
            raise TypeError(f"COST_RATES[{key!r}] must be a number, got {value!r}")
        if key in _PRICE_MAP_KEYS and not (
            isinstance(value, dict)
            and all(
                _is_number(price) for price in cast(dict[str, object], value).values()
            )
        ):
            raise TypeError(
                f"COST_RATES[{key!r}] must be an object of numbers, got {value!r}"
            )


def _merge_into(rates: dict[str, object], overrides: Mapping[str, object]) -> None:
    for key, value in overrides.items():
        existing = rates.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            rates[key] = {**existing, **value}
        else:
            rates[key] = copy.deepcopy(value)


def node_hourly_price(
    instance_type: str | None,
    rates: CostRates,
    *,
    cpu_capacity: float | None = None,
    memory_gib: float | None = None,
) -> float:
    """Hourly on-demand price for a node instance type, from ``rates``.

    Prefers the exact AWS price for the instance type, then the family's
    ``.xlarge`` price scaled by size, then vCPU/memory capacity at the
    standard compute rates when the instance type isn't recognized.
    """
    if instance_type is not None:
        exact = rates.get("node_instance_hourly", {}).get(instance_type)
        if exact is not None:
            return exact
    family, _, size = (instance_type or "").partition(".")
    xlarge_price = rates["node_family_xlarge_hourly"].get(family)
    factor = rates["node_size_factor"].get(size)
    if xlarge_price is not None and factor is not None:
        return xlarge_price * factor
    cpu = cpu_capacity or 0.0
    mem = memory_gib or 0.0
    return cpu * rates["vcpu_hour"] + mem * rates["memory_gib_hour"]


def tiered_monthly_cost(amount: float, tiers: Sequence[PriceTier]) -> float:
    """Monthly USD for ``amount`` units priced in ascending ``tiers``."""
    cost = 0.0
    floor = 0.0
    for tier in tiers:
        cap = tier["up_to"]
        top = amount if cap is None else min(amount, cap)
        if top > floor:
            cost += (top - floor) * tier["usd_month"]
        if cap is None or amount <= cap:
            break
        floor = cap
    return cost


def volume_monthly_cost(gib: float, rates: CostRates) -> float:
    """Monthly USD for one EBS volume of ``gib`` GiB, including any IOPS and
    throughput its StorageClass provisions beyond what the volume type
    includes. Without volume details, only capacity is priced.
    """
    cost = gib * rates["storage_gib_month"]
    volume = rates.get("storage_volume")
    if volume is None:
        return cost
    is_gp3 = volume["volume_type"] == "gp3"
    if volume["iops"] is not None:
        iops = volume["iops"]
    elif volume["iops_per_gib"] is not None:
        iops = volume["iops_per_gib"] * gib
    else:
        iops = GP3_BASELINE_IOPS if is_gp3 else 0.0
    throughput = volume["throughput_mibps"]
    if throughput is None:
        throughput = GP3_BASELINE_THROUGHPUT_MIBPS if is_gp3 else 0.0
    cost += tiered_monthly_cost(iops, rates.get("storage_iops_tiers", []))
    cost += tiered_monthly_cost(throughput, rates.get("storage_throughput_tiers", []))
    return cost
