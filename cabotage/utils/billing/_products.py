"""Stripe product, price, and meter IDs for Cabotage billing.

These are test-mode IDs. Production IDs should be configured via environment
variables or a separate config — never hardcode live keys.

Generated with Stripe CLI and Claude.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StripePrice:
    product_id: str
    price_id: str
    unit_amount_decimal: str  # in cents
    description: str


@dataclass(frozen=True)
class StripeMeter:
    meter_id: str
    event_name: str
    product_id: str
    price_id: str
    unit_amount_decimal: str  # in cents
    unit_label: str
    description: str


PlanTier = Literal["hobby", "indie", "team", "business"]

# ---------------------------------------------------------------------------
# Subscription Plans
# ---------------------------------------------------------------------------

# PLAN_FREE = StripePrice(
#     product_id="prod_UBeh9r3aZ5rs3d",
#     price_id="price_1TDHOTBO5ixFYChuOFTh3WkB",
#     unit_amount_decimal="0",
#     description="Cabotage Free — 2 sandbox services, card required",
# )

PLAN_HOBBY = StripePrice(
    product_id="prod_UFaYNp1S3JuiFA",
    price_id="price_1TH5NgLAb29s0LGgEeyB07d7",
    unit_amount_decimal="400",
    description="Cabotage Hobby — $4/mo, $5 usage credit",
)

PLAN_INDIE = StripePrice(
    product_id="prod_UFaYhhgeWVmOyI",
    price_id="price_1TH5NiLAb29s0LGgk76GK6Jq",
    unit_amount_decimal="900",
    description="Cabotage Indie — $9/mo, $12 usage credit",
)

PLAN_TEAM = StripePrice(
    product_id="prod_UFaYljMBcbwjw7",
    price_id="price_1TH5NjLAb29s0LGgzCzhI4TR",
    unit_amount_decimal="2900",
    description="Cabotage Team — $29/mo, $30 usage credit, unlimited seats",
)

PLAN_BUSINESS = StripePrice(
    product_id="prod_UFaYvC86ojvEJ4",
    price_id="price_1TH5NjLAb29s0LGg9IfA1tQk",
    unit_amount_decimal="25000",
    description="Cabotage Business — $250/mo, SSO/SAML, audit logs, priority support",
)

PLANS = {
    # "free": PLAN_FREE,
    "hobby": PLAN_HOBBY,
    "indie": PLAN_INDIE,
    "team": PLAN_TEAM,
    "business": PLAN_BUSINESS,
}

# Included usage credits per plan (cents)
PLAN_CREDITS = {
    # "free": 0,
    "hobby": 500,
    "indie": 1200,
    "team": 3000,
    "business": 0,  # custom
}

# ---------------------------------------------------------------------------
# Metered Usage Products (overage billing via Stripe Billing Meters)
# ---------------------------------------------------------------------------

METER_VCPU_HOURS = StripeMeter(
    meter_id="mtr_test_61UQTLqok68sH3c8f41LAb29s0LGgHgG",
    event_name="vcpu_hours",
    product_id="prod_UFaYUSEamWIoq8",
    price_id="price_1TH5OiLAb29s0LGgfRzJ2yzU",
    unit_amount_decimal="2.0",  # $0.020/vCPU-hr
    unit_label="vCPU-hour",
    description="Compute vCPU usage",
)

METER_RAM_GB_HOURS = StripeMeter(
    meter_id="mtr_test_61UQTLrrIEBtmZFTX41LAb29s0LGgDfk",
    event_name="ram_gb_hours",
    product_id="prod_UFaYwH0RE018Xi",
    price_id="price_1TH5OkLAb29s0LGgbH682NCo",
    unit_amount_decimal="1.0",  # $0.010/GB-hr
    unit_label="GB-hour",
    description="Memory usage",
)

METER_EGRESS_GB = StripeMeter(
    meter_id="mtr_test_61UQTLroIMkKdQiIj41LAb29s0LGg6Bs",
    event_name="egress_gb",
    product_id="prod_UFaYI4ZdO9phJs",
    price_id="price_1TH5OmLAb29s0LGgdu8n81eH",
    unit_amount_decimal="4.5",  # $0.045/GB
    unit_label="GB",
    description="Outbound data transfer",
)

METER_BLOCK_STORAGE_GB = StripeMeter(
    meter_id="mtr_test_61UQTLsFFFFiWKlhd41LAb29s0LGgMKe",
    event_name="block_storage_gb",
    product_id="prod_UFaYeLH5ZBpxaN",
    price_id="price_1TH5OoLAb29s0LGgUrTPY5TM",
    unit_amount_decimal="12.0",  # $0.12/GB-mo
    unit_label="GB-month",
    description="EBS-backed persistent volumes",
)

METER_OBJECT_STORAGE_GB = StripeMeter(
    meter_id="mtr_test_61UQTLtgLRHJAjtow41LAb29s0LGgGTQ",
    event_name="object_storage_gb",
    product_id="prod_UFaYj5edsjzGrQ",
    price_id="price_1TH5OqLAb29s0LGgLFpK2FMj",
    unit_amount_decimal="3.0",  # $0.03/GB-mo
    unit_label="GB-month",
    description="S3-compatible object storage",
)

METER_DB_STORAGE_GB = StripeMeter(
    meter_id="mtr_test_61UQTLubvlCaOntAX41LAb29s0LGgNhY",
    event_name="db_storage_gb",
    product_id="prod_UFaY4EZAiPIWgp",
    price_id="price_1TH5OsLAb29s0LGgMAoKSvgp",
    unit_amount_decimal="15.0",  # $0.15/GB-mo
    unit_label="GB-month",
    description="Managed database storage with backups",
)

METER_POSTGRES_GB_HOURS = StripeMeter(
    meter_id="mtr_test_61UQTLvhq4mEWWyzg41LAb29s0LGg6AS",
    event_name="postgres_gb_hours",
    product_id="prod_UFaYLW0uLExHBp",
    price_id="price_1TH5OuLAb29s0LGgzbvlYinA",
    unit_amount_decimal="1.5",  # $0.015/GB-hr
    unit_label="GB-hour",
    description="Managed PostgreSQL compute",
)

METER_REDIS_GB_HOURS = StripeMeter(
    meter_id="mtr_test_61UQTLw5MRREBbr2P41LAb29s0LGg2dU",
    event_name="redis_gb_hours",
    product_id="prod_UFaZNzpDVXmsBn",
    price_id="price_1TH5OwLAb29s0LGgP8wpjJyE",
    unit_amount_decimal="1.5",  # $0.015/GB-hr
    unit_label="GB-hour",
    description="Managed Redis compute",
)

METER_BUILD_MINUTES = StripeMeter(
    meter_id="mtr_test_61UQTLxGvjRKvs7T441LAb29s0LGgXn6",
    event_name="build_minutes",
    product_id="prod_UFaZYINplE6T9I",
    price_id="price_1TH5OxLAb29s0LGgvEMWAF1t",
    unit_amount_decimal="0.4",  # $0.004/min
    unit_label="minute",
    description="CI/CD build time (first 500/mo free)",
)

METER_TAILSCALE_NODES = StripeMeter(
    meter_id="mtr_test_61UQTLyjcqE4rYlbt41LAb29s0LGgSYy",
    event_name="tailscale_nodes",
    product_id="prod_UFaZVkT5O9rGZI",
    price_id="price_1TH5P0LAb29s0LGgxIeWJQz8",
    unit_amount_decimal="500",  # $5.00/node-mo
    unit_label="node-month",
    description="Tailscale private networking",
)

METERS = {
    "vcpu_hours": METER_VCPU_HOURS,
    "ram_gb_hours": METER_RAM_GB_HOURS,
    "egress_gb": METER_EGRESS_GB,
    "block_storage_gb": METER_BLOCK_STORAGE_GB,
    "object_storage_gb": METER_OBJECT_STORAGE_GB,
    "db_storage_gb": METER_DB_STORAGE_GB,
    "postgres_gb_hours": METER_POSTGRES_GB_HOURS,
    "redis_gb_hours": METER_REDIS_GB_HOURS,
    "build_minutes": METER_BUILD_MINUTES,
    "tailscale_nodes": METER_TAILSCALE_NODES,
}

# Free tier allowances (units per month, applied in app logic before reporting to Stripe)
FREE_TIER_ALLOWANCES = {
    "build_minutes": 500,
}
