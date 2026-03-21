"""Celery tasks for billing."""
from celery import shared_task
from stripe.billing import MeterEvent

from cabotage.server.models.auth import Billing, Organization
from cabotage.utils.billing._products import METERS


def collect_usage(org: Organization) -> dict:
    pass


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

