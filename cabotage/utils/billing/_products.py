"""Stripe product, price, and meter IDs for Cabotage billing.

These are test-mode IDs. Production IDs should be configured via environment
variables or a separate config — never hardcode live keys.

Generated with Stripe CLI and Claude.
"""

from dataclasses import dataclass


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
    product_id="prod_UBeoZs5pnrtguM",
    price_id="price_1TDHV5BO5ixFYChuGvSSZV1o",
    unit_amount_decimal="400",
    description="Cabotage Hobby — $4/mo, $5 usage credit",
)

PLAN_INDIE = StripePrice(
    product_id="prod_UBehdnxe1SF08J",
    price_id="price_1TDHOUBO5ixFYChurzdHe2eO",
    unit_amount_decimal="900",
    description="Cabotage Indie — $9/mo, $12 usage credit",
)

PLAN_TEAM = StripePrice(
    product_id="prod_UBehS4ZhG8QtkU",
    price_id="price_1TDHOUBO5ixFYChuWEacmQci",
    unit_amount_decimal="2900",
    description="Cabotage Team — $29/mo, $30 usage credit, unlimited seats",
)

PLAN_BYOC = StripePrice(
    product_id="prod_UBehqTFk0Ux34e",
    price_id="price_1TDHOVBO5ixFYChuHB1r6fpF",
    unit_amount_decimal="25000",
    description="Cabotage BYOC — $250/mo, deploy into customer cloud",
)

PLANS = {
    # "free": PLAN_FREE,
    "hobby": PLAN_HOBBY,
    "indie": PLAN_INDIE,
    "team": PLAN_TEAM,
    "byoc": PLAN_BYOC,
}

# Included usage credits per plan (cents)
PLAN_CREDITS = {
    # "free": 0,
    "hobby": 500,
    "indie": 1200,
    "team": 3000,
    "byoc": 0,  # custom
}

# ---------------------------------------------------------------------------
# Metered Usage Products (overage billing via Stripe Billing Meters)
# ---------------------------------------------------------------------------

METER_VCPU_HOURS = StripeMeter(
    meter_id="mtr_test_61UMfN8uG0JWtNbpY41BO5ixFYChuFoW",
    event_name="vcpu_hours",
    product_id="prod_UBeiL3m5GTnSJ6",
    price_id="price_1TDHQIBO5ixFYChujseWkxR5",
    unit_amount_decimal="2.0",  # $0.020/vCPU-hr
    unit_label="vCPU-hour",
    description="Compute vCPU usage",
)

METER_RAM_GB_HOURS = StripeMeter(
    meter_id="mtr_test_61UMfN9BgKzUbelBO41BO5ixFYChuS1g",
    event_name="ram_gb_hours",
    product_id="prod_UBeiX02S6VTdyI",
    price_id="price_1TDHQJBO5ixFYChuKwaBoI3i",
    unit_amount_decimal="1.0",  # $0.010/GB-hr
    unit_label="GB-hour",
    description="Memory usage",
)

METER_EGRESS_GB = StripeMeter(
    meter_id="mtr_test_61UMfN9b09e1I9sHj41BO5ixFYChu9FI",
    event_name="egress_gb",
    product_id="prod_UBeigcFTNNa67Q",
    price_id="price_1TDHQKBO5ixFYChu3LxjLkrg",
    unit_amount_decimal="4.5",  # $0.045/GB
    unit_label="GB",
    description="Outbound data transfer",
)

METER_BLOCK_STORAGE_GB = StripeMeter(
    meter_id="mtr_test_61UMfNA1YGF38G6Zg41BO5ixFYChuNA8",
    event_name="block_storage_gb",
    product_id="prod_UBeizN6CddU36t",
    price_id="price_1TDHQKBO5ixFYChuTTcQb2ws",
    unit_amount_decimal="12.0",  # $0.12/GB-mo
    unit_label="GB-month",
    description="EBS-backed persistent volumes",
)

METER_OBJECT_STORAGE_GB = StripeMeter(
    meter_id="mtr_test_61UMfNAlB05DFWRbo41BO5ixFYChuX4C",
    event_name="object_storage_gb",
    product_id="prod_UBeiFA90cIAUCa",
    price_id="price_1TDHQLBO5ixFYChuRn7JJlEw",
    unit_amount_decimal="3.0",  # $0.03/GB-mo
    unit_label="GB-month",
    description="S3-compatible object storage",
)

METER_DB_STORAGE_GB = StripeMeter(
    meter_id="mtr_test_61UMfNAbiXGIXRLFt41BO5ixFYChuP84",
    event_name="db_storage_gb",
    product_id="prod_UBeiUjxWj72ZWD",
    price_id="price_1TDHQLBO5ixFYChuD5IMGwar",
    unit_amount_decimal="15.0",  # $0.15/GB-mo
    unit_label="GB-month",
    description="Managed database storage with backups",
)

METER_POSTGRES_GB_HOURS = StripeMeter(
    meter_id="mtr_test_61UMfNBJkCmK0VjSr41BO5ixFYChuQUy",
    event_name="postgres_gb_hours",
    product_id="prod_UBei4IFTbwbk3S",
    price_id="price_1TDHQMBO5ixFYChug3lFVOcL",
    unit_amount_decimal="1.5",  # $0.015/GB-hr
    unit_label="GB-hour",
    description="Managed PostgreSQL compute",
)

METER_REDIS_GB_HOURS = StripeMeter(
    meter_id="mtr_test_61UMfNByapkQnwSiu41BO5ixFYChuPlo",
    event_name="redis_gb_hours",
    product_id="prod_UBeiOLfQSGqn7D",
    price_id="price_1TDHQMBO5ixFYChu1fffHJiV",
    unit_amount_decimal="1.5",  # $0.015/GB-hr
    unit_label="GB-hour",
    description="Managed Redis compute",
)

METER_BUILD_MINUTES = StripeMeter(
    meter_id="mtr_test_61UMfNCvulcvvpFfy41BO5ixFYChu412",
    event_name="build_minutes",
    product_id="prod_UBeiQqPxCYILLz",
    price_id="price_1TDHQNBO5ixFYChuUYXWYt9m",
    unit_amount_decimal="0.4",  # $0.004/min
    unit_label="minute",
    description="CI/CD build time (first 500/mo free)",
)

METER_TAILSCALE_NODES = StripeMeter(
    meter_id="mtr_test_61UMfNwCbCd5whBQz41BO5ixFYChuV6m",
    event_name="tailscale_nodes",
    product_id="prod_UBekTctqhekpQO",
    price_id="price_1TDHQjBO5ixFYChuKLnxXhj5",
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
