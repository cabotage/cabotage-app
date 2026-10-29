"""Refresh cost rates from the AWS Price List Query API.

:func:`refresh_prices` looks up the on-demand price of every node instance
type seen recently, and the EBS prices for the volume type the backing-service
StorageClass provisions, then stores them as a
:class:`~cabotage.server.models.cost.CostPriceSnapshot` that
:func:`~cabotage.server.applets.cost.rates.effective_rates` layers over the defaults.
"""

import datetime
import json
import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, cast

import boto3
import kubernetes.client
from flask import current_app
from kubernetes.client.exceptions import ApiException

from cabotage.server import db
from cabotage.server import kubernetes as kubernetes_ext
from cabotage.server.applets.cost.constants import (
    DEFAULT_CLASS_ANNOTATION,
    DEFAULT_VOLUME_TYPE,
    EC2_INSTANCE_FILTERS,
    GP3_BASELINE_IOPS,
    GP3_BASELINE_THROUGHPUT_MIBPS,
    IO2_IOPS_TIERS,
    NODE_LOOKBACK,
    STORAGE_UNITS,
    THROUGHPUT_UNIT_MIBPS,
)
from cabotage.server.applets.cost.exceptions import AwsPricingError
from cabotage.server.applets.cost.helpers import optional_float
from cabotage.server.applets.cost.types import (
    PriceListItem,
    PriceTier,
    SnapshotRates,
    StorageVolume,
    VolumeRates,
)
from cabotage.server.models.cost import CostNode, CostPriceSnapshot

if TYPE_CHECKING:
    from mypy_boto3_pricing.client import PricingClient
    from mypy_boto3_pricing.type_defs import FilterTypeDef

log = logging.getLogger(__name__)


def refresh_prices(client: PricingClient | None = None) -> CostPriceSnapshot:
    config = current_app.config
    region: str = config["COST_AWS_REGION"]
    if client is None:
        api_region: str = config["COST_AWS_PRICING_API_REGION"]
        client = boto3.client("pricing", region_name=api_region)
    now = datetime.datetime.now(datetime.UTC)

    rates: SnapshotRates = {
        "source": {
            "basis": "AWS Price List API, on-demand",
            "region": region,
            "checked": now.date().isoformat(),
        }
    }
    instance_types = {
        node.instance_type
        for node in CostNode.query.filter(
            CostNode.instance_type.isnot(None),
            CostNode.last_seen >= now - NODE_LOOKBACK,
        ).all()
        if node.instance_type is not None
    }
    node_prices = fetch_instance_prices(client, region, instance_types)
    if node_prices:
        rates["node_instance_hourly"] = node_prices

    volume = read_storage_volume(config.get("COST_STORAGE_CLASS"))
    if volume is not None:
        volume_rates = fetch_volume_prices(client, region, volume["volume_type"])
        rates["storage_gib_month"] = volume_rates["storage_gib_month"]
        rates["storage_iops_tiers"] = volume_rates["storage_iops_tiers"]
        rates["storage_throughput_tiers"] = volume_rates["storage_throughput_tiers"]
        rates["storage_volume"] = volume

    snapshot = CostPriceSnapshot(fetched_at=now, region=region, rates=rates)
    db.session.add(snapshot)
    db.session.commit()
    return snapshot


def fetch_instance_prices(
    client: PricingClient, region: str, instance_types: Iterable[str]
) -> dict[str, float]:
    """Hourly on-demand USD per instance type. Types the Price List doesn't
    know are skipped, so they keep falling back to the family table.
    """
    prices: dict[str, float] = {}
    for instance_type in sorted(instance_types):
        items = _get_products(
            client,
            {"regionCode": region, "instanceType": instance_type}
            | EC2_INSTANCE_FILTERS,
        )
        if not items:
            log.warning("No AWS price for %s in %s", instance_type, region)
            continue
        quotes = {_on_demand_price(item) for item in items}
        if len(quotes) != 1:
            raise AwsPricingError(
                f"Expected one on-demand price for {instance_type} in {region}, "
                f"got {sorted(quotes)}"
            )
        unit, usd = quotes.pop()
        if unit != "Hrs":
            raise AwsPricingError(f"Unexpected unit {unit!r} for {instance_type}")
        prices[instance_type] = usd
    return prices


def fetch_volume_prices(
    client: PricingClient, region: str, volume_type: str
) -> VolumeRates:
    storage: float | None = None
    iops_by_group: dict[str, float] = {}
    throughput_mibps: float | None = None
    for item in _get_products(
        client, {"regionCode": region, "volumeApiName": volume_type}
    ):
        family = item["product"]["productFamily"]
        unit, usd = _on_demand_price(item)
        if family == "Storage" and unit in STORAGE_UNITS:
            storage = usd
        elif family == "System Operation" and unit == "IOPS-Mo":
            iops_by_group[item["product"]["attributes"]["group"]] = usd
        elif family == "Provisioned Throughput" and unit in THROUGHPUT_UNIT_MIBPS:
            throughput_mibps = usd / THROUGHPUT_UNIT_MIBPS[unit]
        else:
            raise AwsPricingError(
                f"Unexpected {volume_type} product {family!r} priced per {unit!r}"
            )
    if storage is None:
        raise AwsPricingError(f"No storage price for {volume_type} in {region}")

    iops_tiers: list[PriceTier] = []
    if volume_type == "gp3" and "EBS IOPS" in iops_by_group:
        iops_tiers = [
            {"up_to": GP3_BASELINE_IOPS, "usd_month": 0.0},
            {"up_to": None, "usd_month": iops_by_group["EBS IOPS"]},
        ]
    elif volume_type == "io2":
        if missing := [g for g, _ in IO2_IOPS_TIERS if g not in iops_by_group]:
            raise AwsPricingError(f"Missing io2 IOPS tiers {missing} in {region}")
        iops_tiers = [
            {"up_to": up_to, "usd_month": iops_by_group[group]}
            for group, up_to in IO2_IOPS_TIERS
        ]
    elif len(iops_by_group) == 1:
        iops_tiers = [{"up_to": None, "usd_month": next(iter(iops_by_group.values()))}]
    elif iops_by_group:
        raise AwsPricingError(
            f"Unexpected IOPS tiers for {volume_type}: {sorted(iops_by_group)}"
        )

    throughput_tiers: list[PriceTier] = []
    if throughput_mibps is not None:
        free = GP3_BASELINE_THROUGHPUT_MIBPS if volume_type == "gp3" else 0.0
        throughput_tiers = [
            {"up_to": free, "usd_month": 0.0},
            {"up_to": None, "usd_month": throughput_mibps},
        ]

    return {
        "storage_gib_month": storage,
        "storage_iops_tiers": iops_tiers,
        "storage_throughput_tiers": throughput_tiers,
    }


def read_storage_volume(storage_class_name: str | None) -> StorageVolume | None:
    """How the backing-service StorageClass provisions EBS volumes: the named
    class, or the cluster default. ``None`` if it isn't an EBS class.
    """
    if not current_app.config.get("KUBERNETES_ENABLED"):
        return None
    storage_api = kubernetes.client.StorageV1Api(kubernetes_ext.kubernetes_client)
    try:
        storage_classes = storage_api.list_storage_class().items
    except ApiException:
        log.warning("Could not list StorageClasses", exc_info=True)
        return None
    storage_class = _select_storage_class(storage_classes, storage_class_name)
    if storage_class is None:
        log.warning("StorageClass %s not found", storage_class_name or "(default)")
        return None
    default_type = DEFAULT_VOLUME_TYPE.get(storage_class.provisioner)
    if default_type is None:
        return None
    # EBS CSI parameter names are case-insensitive.
    params = {k.lower(): v for k, v in (storage_class.parameters or {}).items()}
    return {
        "volume_type": params.get("type", default_type).lower(),
        "iops": optional_float(params.get("iops")),
        "iops_per_gib": optional_float(params.get("iopspergb")),
        "throughput_mibps": optional_float(params.get("throughput")),
    }


def _select_storage_class(
    classes: Iterable[kubernetes.client.V1StorageClass], name: str | None
) -> kubernetes.client.V1StorageClass | None:
    for storage_class in classes:
        metadata = storage_class.metadata
        if metadata is None:
            continue
        if name:
            if metadata.name == name:
                return storage_class
        elif (metadata.annotations or {}).get(DEFAULT_CLASS_ANNOTATION) == "true":
            return storage_class
    return None


def _get_products(
    client: PricingClient, filters: Mapping[str, str]
) -> list[PriceListItem]:
    term_filters: list[FilterTypeDef] = [
        {"Type": "TERM_MATCH", "Field": field, "Value": value}
        for field, value in filters.items()
    ]
    items: list[PriceListItem] = []
    for page in client.get_paginator("get_products").paginate(
        ServiceCode="AmazonEC2", FormatVersion="aws_v1", Filters=term_filters
    ):
        items.extend(cast(PriceListItem, json.loads(raw)) for raw in page["PriceList"])
    return items


def _on_demand_price(item: PriceListItem) -> tuple[str, float]:
    dimensions = [
        dimension
        for term in item["terms"].get("OnDemand", {}).values()
        for dimension in term["priceDimensions"].values()
    ]
    if len(dimensions) != 1:
        raise AwsPricingError(
            f"Expected one on-demand price for SKU {item['product']['sku']}, "
            f"got {len(dimensions)}"
        )
    return dimensions[0]["unit"], float(dimensions[0]["pricePerUnit"]["USD"])
