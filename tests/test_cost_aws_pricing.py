import datetime
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from cabotage.server import db
from cabotage.server.applets.cost.aws_pricing import (
    fetch_instance_prices,
    fetch_volume_prices,
    refresh_prices,
)
from cabotage.server.applets.cost.constants import DEFAULT_RATES
from cabotage.server.applets.cost.exceptions import AwsPricingError
from cabotage.server.applets.cost.rates import (
    effective_rates,
    node_hourly_price,
    volume_monthly_cost,
)
from cabotage.server.models.cost import CostNode, CostPriceSnapshot
from cabotage.server.wsgi import app as _app

REGION = "us-east-2"
LINUX_ON_DEMAND = {
    "operatingSystem": "Linux",
    "tenancy": "Shared",
    "preInstalledSw": "NA",
    "capacitystatus": "Used",
    "marketoption": "OnDemand",
}


def _item(family, attributes, unit, usd):
    """One GetProducts PriceList entry, shaped like the published us-east-2
    offer file (units and prices below are copied from it).
    """
    sku = uuid.uuid4().hex[:16].upper()
    return {
        "product": {
            "productFamily": family,
            "sku": sku,
            "attributes": {"regionCode": REGION, **attributes},
        },
        "terms": {
            "OnDemand": {
                f"{sku}.JRTCKXETXF": {
                    "priceDimensions": {
                        f"{sku}.JRTCKXETXF.6YS6EN2CT7": {
                            "unit": unit,
                            "pricePerUnit": {"USD": usd},
                        }
                    }
                }
            }
        },
    }


def _instance(instance_type, usd, **overrides):
    return _item(
        "Compute Instance",
        {"instanceType": instance_type, **LINUX_ON_DEMAND, **overrides},
        "Hrs",
        usd,
    )


GP3 = [
    _item("Storage", {"volumeApiName": "gp3"}, "GB-Mo", "0.0800000000"),
    _item(
        "System Operation",
        {"volumeApiName": "gp3", "group": "EBS IOPS"},
        "IOPS-Mo",
        "0.0050000000",
    ),
    _item(
        "Provisioned Throughput",
        {"volumeApiName": "gp3", "group": "EBS Throughput"},
        "GiBps-mo",
        "40.9600000000",
    ),
]
IO2 = [
    _item("Storage", {"volumeApiName": "io2"}, "GB-month", "0.1250000000"),
    _item(
        "System Operation",
        {"volumeApiName": "io2", "group": "EBS IOPS"},
        "IOPS-Mo",
        "0.0650000000",
    ),
    _item(
        "System Operation",
        {"volumeApiName": "io2", "group": "EBS IOPS Tier 2"},
        "IOPS-Mo",
        "0.0455000000",
    ),
    _item(
        "System Operation",
        {"volumeApiName": "io2", "group": "EBS IOPS Tier 3"},
        "IOPS-Mo",
        "0.0318500000",
    ),
]


class FakePricing:
    """Answers GetProducts by TERM_MATCHing filters against product attributes."""

    def __init__(self, items):
        self.items = items

    def get_paginator(self, name):
        assert name == "get_products"
        return self

    def paginate(self, *, ServiceCode, FormatVersion, Filters):
        assert (ServiceCode, FormatVersion) == ("AmazonEC2", "aws_v1")
        matches = [
            item
            for item in self.items
            if all(
                f["Type"] == "TERM_MATCH"
                and item["product"]["attributes"].get(f["Field"]) == f["Value"]
                for f in Filters
            )
        ]
        yield {"PriceList": [json.dumps(item) for item in matches]}


def _rates_with_volume(volume_rates, **volume):
    return {
        **DEFAULT_RATES,
        **volume_rates,
        "storage_volume": {
            "volume_type": "gp3",
            "iops": None,
            "iops_per_gib": None,
            "throughput_mibps": None,
            **volume,
        },
    }


class TestFetchInstancePrices:
    def test_picks_linux_on_demand_and_skips_unknown_types(self):
        client = FakePricing(
            [
                _instance("m8g.xlarge", "0.1795200000"),
                _instance("m8g.xlarge", "0.3635200000", operatingSystem="Windows"),
                _instance(
                    "m8g.xlarge",
                    "0.1000000000",
                    capacitystatus="AllocatedCapacityReservation",
                ),
                _instance("m8g.xlarge", "0.2500000000", marketoption="CapacityBlock"),
            ]
        )

        prices = fetch_instance_prices(client, REGION, {"m8g.xlarge", "zz9.huge"})

        assert prices == {"m8g.xlarge": pytest.approx(0.17952)}

    def test_conflicting_prices_raise(self):
        client = FakePricing(
            [
                _instance("m8g.xlarge", "0.1795200000"),
                _instance("m8g.xlarge", "0.1900000000"),
            ]
        )

        with pytest.raises(AwsPricingError):
            fetch_instance_prices(client, REGION, {"m8g.xlarge"})


class TestVolumePrices:
    def test_gp3_bills_only_iops_and_throughput_above_baseline(self):
        gp3 = fetch_volume_prices(FakePricing(GP3), REGION, "gp3")

        baseline = _rates_with_volume(gp3)
        provisioned = _rates_with_volume(gp3, iops=6000.0, throughput_mibps=250.0)

        assert volume_monthly_cost(100, baseline) == pytest.approx(8.0)
        # 100 GiB * $0.08 + 3000 extra IOPS * $0.005 + 125 extra MiB/s * $0.04
        assert volume_monthly_cost(100, provisioned) == pytest.approx(28.0)

    def test_gp3_iops_per_gib_scales_with_size(self):
        gp3 = fetch_volume_prices(FakePricing(GP3), REGION, "gp3")
        rates = _rates_with_volume(gp3, iops_per_gib=50.0)

        # 5000 IOPS: 2000 above the baseline.
        assert volume_monthly_cost(100, rates) == pytest.approx(8.0 + 10.0)

    def test_io2_iops_are_tiered(self):
        io2 = fetch_volume_prices(FakePricing(IO2), REGION, "io2")
        rates = _rates_with_volume(io2, volume_type="io2", iops=70000.0)

        expected = 10 * 0.125 + 32000 * 0.065 + 32000 * 0.0455 + 6000 * 0.03185
        assert volume_monthly_cost(10, rates) == pytest.approx(expected)

    def test_unknown_product_shape_raises(self):
        client = FakePricing(
            [*GP3, _item("Storage Snapshot", {"volumeApiName": "gp3"}, "GB-Mo", "0.05")]
        )

        with pytest.raises(AwsPricingError):
            fetch_volume_prices(client, REGION, "gp3")


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    with _app.app_context():
        yield _app
    _app.config["COST_AWS_PRICING_ENABLED"] = False
    _app.config["COST_RATES"] = None
    _app.config["KUBERNETES_ENABLED"] = False


@pytest.fixture
def clean_pricing(app):
    yield
    db.session.rollback()
    CostPriceSnapshot.query.delete()
    CostNode.query.filter(CostNode.name.like("pricing-test-%")).delete()
    db.session.commit()


@pytest.mark.usefixtures("clean_pricing")
class TestEffectiveRates:
    def test_snapshot_layers_between_defaults_and_config(self, app):
        db.session.add(
            CostPriceSnapshot(
                fetched_at=datetime.datetime.now(datetime.UTC),
                region=REGION,
                rates={
                    "node_instance_hourly": {"m8g.xlarge": 0.2},
                    "storage_gib_month": 0.09,
                    "egress_gb": 0.07,
                },
            )
        )
        db.session.commit()
        app.config["COST_RATES"] = {"storage_gib_month": 0.1}

        app.config["COST_AWS_PRICING_ENABLED"] = False
        disabled = effective_rates()
        app.config["COST_AWS_PRICING_ENABLED"] = True
        enabled = effective_rates()

        assert node_hourly_price("m8g.xlarge", disabled) == pytest.approx(0.17952)
        assert node_hourly_price("m8g.xlarge", enabled) == pytest.approx(0.2)
        assert enabled["egress_gb"] == pytest.approx(0.07)
        assert enabled["storage_gib_month"] == pytest.approx(0.1)

    def test_family_override_beats_snapshot_instance_price(self, app):
        db.session.add(
            CostPriceSnapshot(
                fetched_at=datetime.datetime.now(datetime.UTC),
                region=REGION,
                rates={"node_instance_hourly": {"m8g.xlarge": 0.2, "c8g.xlarge": 0.15}},
            )
        )
        db.session.commit()
        app.config["COST_AWS_PRICING_ENABLED"] = True
        app.config["COST_RATES"] = {"node_family_xlarge_hourly": {"m8g": 0.12}}

        rates = effective_rates()

        assert node_hourly_price("m8g.xlarge", rates) == pytest.approx(0.12)
        assert node_hourly_price("c8g.xlarge", rates) == pytest.approx(0.15)


@pytest.mark.usefixtures("clean_pricing")
class TestRefreshPrices:
    def test_prices_recent_nodes_and_default_storage_class(self, app):
        now = datetime.datetime.now(datetime.UTC)
        db.session.add_all(
            [
                CostNode(
                    name="pricing-test-current",
                    instance_type="m8g.xlarge",
                    last_seen=now,
                ),
                CostNode(
                    name="pricing-test-gone",
                    instance_type="c8g.large",
                    last_seen=now - datetime.timedelta(days=30),
                ),
            ]
        )
        db.session.commit()
        app.config["KUBERNETES_ENABLED"] = True
        storage_classes = [
            SimpleNamespace(
                metadata=SimpleNamespace(name="fast", annotations=None),
                provisioner="ebs.csi.aws.com",
                parameters={"type": "io2", "iops": "20000"},
            ),
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="gp3",
                    annotations={"storageclass.kubernetes.io/is-default-class": "true"},
                ),
                provisioner="ebs.csi.aws.com",
                parameters={"Type": "gp3", "Throughput": "250"},
            ),
        ]
        storage_api = MagicMock()
        storage_api.list_storage_class.return_value = SimpleNamespace(
            items=storage_classes
        )
        client = FakePricing(
            [
                _instance("m8g.xlarge", "0.1795200000"),
                _instance("c8g.large", "0.08"),
                *GP3,
            ]
        )

        with (
            patch("cabotage.server.applets.cost.aws_pricing.kubernetes_ext"),
            patch(
                "cabotage.server.applets.cost.aws_pricing.kubernetes.client.StorageV1Api",
                return_value=storage_api,
            ),
        ):
            snapshot = refresh_prices(client)

        assert CostPriceSnapshot.latest() == snapshot
        assert snapshot.rates["node_instance_hourly"] == {"m8g.xlarge": 0.17952}
        assert snapshot.rates["storage_volume"] == {
            "volume_type": "gp3",
            "iops": None,
            "iops_per_gib": None,
            "throughput_mibps": 250.0,
        }
        app.config["COST_AWS_PRICING_ENABLED"] = True
        # 125 MiB/s above the gp3 baseline at $0.04.
        assert volume_monthly_cost(100, effective_rates()) == pytest.approx(13.0)

    def test_storage_class_failure_keeps_node_prices(self, app):
        db.session.add(
            CostNode(
                name="pricing-test-current",
                instance_type="m8g.xlarge",
                last_seen=datetime.datetime.now(datetime.UTC),
            )
        )
        db.session.commit()
        app.config["KUBERNETES_ENABLED"] = True
        storage_api = MagicMock()
        storage_api.list_storage_class.side_effect = ApiException(status=403)

        with (
            patch("cabotage.server.applets.cost.aws_pricing.kubernetes_ext"),
            patch(
                "cabotage.server.applets.cost.aws_pricing.kubernetes.client.StorageV1Api",
                return_value=storage_api,
            ),
        ):
            snapshot = refresh_prices(
                FakePricing([_instance("m8g.xlarge", "0.1795200000")])
            )

        assert CostPriceSnapshot.latest() == snapshot
        assert snapshot.rates["node_instance_hourly"] == {"m8g.xlarge": 0.17952}
        assert "storage_volume" not in snapshot.rates
