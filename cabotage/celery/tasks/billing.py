"""Celery tasks for billing."""
from celery import shared_task
from stripe.billing import MeterEvent

from cabotage.server.models.auth import Billing, Organization
from cabotage.utils.billing._products import METERS


def collect_usage(org: Organization) -> dict:
    usage_data: dict[str, float] = {
        "vcpu_hrs": ...,
        "ram_gb_hrs": ...,
        "egress_gb": ...,
        "block_storage_gb": ...,
        "object_storage_gb": ...,
        "db_storage_gb": ...,
        "postgres_gb_hrs": ...,
        "redis_gb_hrs": ...,
        "build_minutes": ...,
        "tailscale_nodes": ...,
    }
    return usage_data


@shared_task()
def usage() -> None:
    for org in Organization.query.join(Billing).filter(
        Billing.stripe_customer_id.isnot(None),
        Billing.stripe_sub_status == "active"
    ):
        usage = collect_usage(org)

        for k, v in usage.items():
            if v <= 0:
                continue
            meter = METERS[k]
            MeterEvent.create(
                event_name=meter.event_name,
                payload={
                    "stripe_customer_id": org.billing.stripe_customer_id,
                    "value": str(v)
                }
            )

