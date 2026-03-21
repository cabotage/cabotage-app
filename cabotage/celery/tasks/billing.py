"""Celery tasks for billing.

Runs hourly via Celery Beat. Collects resource usage per org
and reports it to Stripe Billing Meters.
"""

import logging

from celery import shared_task
from stripe.billing import MeterEvent

from cabotage.server.models.auth import Billing, Organization
from cabotage.utils.billing.metering import collect_usage
from cabotage.utils.billing._products import METERS

logger = logging.getLogger(__name__)


@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def report_usage() -> None:
    """Report metered usage to Stripe for all active billing orgs."""
    for org in Organization.query.join(Billing).filter(
        Billing.stripe_customer_id.isnot(None),
        Billing.stripe_sub_status == "active",
    ):
        try:
            usage = collect_usage(org)
            for k, v in usage.items():
                if v <= 0:
                    continue
                meter = METERS[k]
                MeterEvent.create(
                    event_name=meter.event_name,
                    payload={
                        "stripe_customer_id": org.billing.stripe_customer_id,
                        "value": str(v),
                    },
                )
        except Exception:
            logger.exception("Failed to report usage for org %s", org.id)
