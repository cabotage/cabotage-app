"""Stripe module for stripe things."""

import logging
from datetime import datetime

from flask_login import login_required
from stripe import checkout, Customer, Subscription, Webhook, SignatureVerificationError, PaymentMethod, Invoice
from stripe.billing import Meter

from flask import current_app, jsonify, Blueprint, Response, request

from cabotage.server import db
from cabotage.server.models import Organization
from cabotage.server.models.auth import Billing, BillingWebhookEvent
from cabotage.utils.billing._products import PLANS, PLAN_CREDITS, PlanTier, METERS

logger = logging.getLogger(__name__)

stripe_blueprint = Blueprint(
    "billing",
    __name__,
    url_prefix="/billing",
)


@stripe_blueprint.route("/checkout-session", methods=["POST"])
@login_required
def create_checkout_session() -> str | Response:
    """Create a Stripe checkout session for payment."""
    scheme = current_app.config["EXT_PREFERRED_URL_SCHEME"]
    server = current_app.config["EXT_SERVER_NAME"]
    try:
        session = checkout.Session.create(
            ui_mode="custom",
            line_items=[],
            mode="payment",
            return_url=f"{scheme}://{server}/billing/?session_id={{CHECKOUT_SESSION_ID}}",
        )
    except Exception as exc:
        logger.exception("Error creating Stripe checkout session: %s", exc)
        return str(exc)
    return jsonify(clientSecret=session.client_secret)


@stripe_blueprint.route("/webhook", methods=["POST"])
def receive_stripe_webhook() -> tuple[str, int]:
    """Handle incoming Stripe webhooks."""
    payload = request.get_data()
    header = request.headers.get("Stripe-Signature")

    try:
        event = Webhook.construct_event(
            payload, header, current_app.config["STRIPE_WEBHOOK_SECRET"]
        )
    except ValueError:
        logger.exception("Invalid Stripe webhook payload")
        return "Invalid payload", 400
    except SignatureVerificationError:
        logger.exception("Invalid Stripe webhook signature")
        return "Invalid signature", 400

    logger.info("Stripe webhook received and verified: %s", event.type)
    _seen = BillingWebhookEvent.query.filter_by(stripe_event_id=event.id).first()
    if _seen:
        logger.info("Already processed event %s, skipping", event.id)
        return "Already processed", 200

    webhook_event = BillingWebhookEvent(
        stripe_event_id=event.id, event_type=event.type, payload=event.data
    )
    db.session.add(webhook_event)

    match event.type:
        case "customer.subscription.created" | "customer.subscription.updated":
            handle_subscription_change(event.data.object)
        case "customer.subscription.deleted":
            handle_subscription_canceled(event.data.object)
        case "invoice.paid":
            handle_invoice_paid(event.data.object)
        case "invoice.payment_failed":
            handle_payment_failed(event.data.object)
        case "invoice.created":
            handle_invoice_created(event.data.object)
        case "setup_intent.succeeded":
            handle_setup_intent_succeeded(event.data.object)

    db.session.commit()
    return "", 200


# ---------------------------------------------------------------------------
# Customer management
# ---------------------------------------------------------------------------

def create_or_get_customer(org: Organization) -> Customer:
    """Create or retrieve a Stripe customer for an organization."""
    org_bill = org.billing
    if org_bill and org_bill.stripe_customer_id:
        logger.debug("Found existing Stripe customer for org %s", org.id)
        return Customer.retrieve(org_bill.stripe_customer_id)

    customer = Customer.create(
        name=org.name, metadata={"org_id": str(org.id), "org_slug": org.slug}
    )

    if not org_bill:
        logger.debug("Creating new Billing record for org %s", org.id)
        org_bill = Billing(org_id=org.id)
        db.session.add(org_bill)

    org_bill.stripe_customer_id = customer.id
    db.session.commit()
    logger.debug("Created Stripe customer for org %s", org.id)
    return customer


def get_default_payment_method(customer_id: str) -> dict | None:
    """Retrieve the default payment method for a Stripe customer."""
    customer = Customer.retrieve(
        customer_id,
        expand=["invoice_settings.default_payment_method"],
    )
    if not customer.invoice_settings:
        return None
    payment_method = customer.invoice_settings.get("default_payment_method")
    if not payment_method or isinstance(payment_method, str):
        return None
    result = {"type": payment_method.type}
    if payment_method.type == "card" and payment_method.card:
        result.update({
            "brand": payment_method.card.brand,
            "last4": payment_method.card.last4,
            "exp_month": payment_method.card.exp_month,
            "exp_year": payment_method.card.exp_year,
        })
    elif payment_method.type == "us_bank_account" and payment_method.us_bank_account:
        result.update({
            "brand": payment_method.us_bank_account.bank_name or "Bank",
            "last4": payment_method.us_bank_account.last4,
            "account_type": payment_method.us_bank_account.account_type,
        })
    else:
        result.update({"brand": payment_method.type, "last4": "••••"})
    return result


def get_invoices(customer_id: str, limit: int = 10) -> list[dict]:
    """Fetch recent invoices for a Stripe customer."""
    invoice_list = Invoice.list(customer=customer_id, limit=limit)
    invoices = []
    for invoice in invoice_list.data:
        desc = "Subscription"
        if invoice.lines and invoice.lines.data:
            desc = invoice.lines.data[0].description or desc
        invoices.append({
            "date": datetime.fromtimestamp(invoice.created).strftime("%Y-%m-%d"),
            "description": desc,
            "amount": f"${invoice.amount_due / 100:.2f}",
            "status": invoice.status,
            "pdf_url": invoice.invoice_pdf or "",
        })
    return invoices


METER_DISPLAY = {
    "vcpu_hours": ("vCPU Hours", "cpu", "hr"),
    "ram_gb_hours": ("RAM (GB-hours)", "hard-drive", "GB-hr"),
    "egress_gb": ("Egress (GB)", "upload", "GB"),
    "build_minutes": ("Build Minutes", "tool", "min"),
    "block_storage_gb": ("Block Storage (GB)", "hard-drive", "GB-mo"),
    "object_storage_gb": ("Object Storage (GB)", "hard-drive", "GB-mo"),
    "db_storage_gb": ("DB Storage (GB)", "layers", "GB-mo"),
    "postgres_gb_hours": ("PostgreSQL (GB-hours)", "layers", "GB-hr"),
    "redis_gb_hours": ("Redis (GB-hours)", "layers", "GB-hr"),
    "tailscale_nodes": ("Tailscale Nodes", "globe", "nodes"),
}


def get_usage(customer_id: str, subscription_id: str) -> list[dict]:
    """Fetch current-period usage from Stripe Billing Meters."""
    import time

    sub = Subscription.retrieve(subscription_id)
    # Stripe removed current_period_start/end — derive from billing_cycle_anchor
    # https://docs.stripe.com/changelog/basil/2025-03-31/deprecate-subscription-current-period-start-and-end
    anchor = sub.billing_cycle_anchor
    now = int(time.time())
    # Walk forward from anchor in ~30-day intervals to find current period
    period_start = anchor
    while period_start + 2592000 < now:  # 30 days in seconds
        period_start += 2592000
    period_end = now

    usage = []
    for meter_key, meter in METERS.items():
        label, icon, unit = METER_DISPLAY.get(meter_key, (meter_key, "activity", "units"))
        rate = float(meter.unit_amount_decimal) / 100

        total_used = 0
        try:
            summaries = Meter.list_event_summaries(
                meter.meter_id,
                customer=customer_id,
                start_time=period_start,
                end_time=period_end,
            )
            if summaries.data:
                total_used = sum(s.aggregated_value for s in summaries.data)
        except Exception:
            logger.debug("Could not fetch meter summaries for %s", meter_key)

        if total_used > 0:
            used_display = int(total_used) if total_used == int(total_used) else round(total_used, 2)
            cost = round(total_used * rate, 2)
            usage.append({
                "label": label,
                "used": used_display,
                "cost": cost,
                "icon": icon,
                "unit": unit,
                "rate": rate,
            })

    return usage


# ---------------------------------------------------------------------------
# Subscription management
# ---------------------------------------------------------------------------

def create_sub(org: Organization, tier: PlanTier):
    """Create a Stripe subscription with incomplete payment."""
    customer = create_or_get_customer(org)
    _plan = PLANS[tier]

    products = [{"price": _plan.price_id}]
    for meter in METERS.values():
        products.append({"price": meter.price_id})

    sub = Subscription.create(
        customer=customer.id,
        items=products,
        payment_behavior="default_incomplete",
        expand=["latest_invoice.payment_intent"],
        metadata={"org_id": str(org.id), "org_slug": org.slug, "plan_tier": tier},
    )
    # no db update here, wait for our webhook to do it
    return sub


# ---------------------------------------------------------------------------
# Webhook handlers
# ---------------------------------------------------------------------------

def handle_subscription_change(subscription) -> None:
    """Handle subscription created or updated events."""
    org_id = subscription.metadata.get("org_id")
    if not org_id:
        logger.warning("Subscription %s has no org_id in metadata", subscription.id)
        return

    billing = Billing.query.filter_by(org_id=org_id).first()
    if not billing:
        logger.warning("No billing record for org %s", org_id)
        return

    billing.stripe_sub_id = subscription.id
    billing.stripe_sub_status = subscription.status
    billing.stripe_sub_plan = subscription.metadata.get(
        "plan_tier", billing.stripe_sub_plan
    )
    logger.info(
        "Updated subscription for org %s: status=%s", org_id, subscription.status
    )


def handle_subscription_canceled(subscription) -> None:
    """Handle subscription deleted — reset org to free tier."""
    org_id = subscription.metadata.get("org_id")
    if not org_id:
        logger.warning("Subscription %s has no org_id in metadata", subscription.id)
        return

    billing = Billing.query.filter_by(org_id=org_id).first()
    if not billing:
        logger.warning("No billing record for org %s", org_id)
        return

    billing.stripe_sub_status = "canceled"
    billing.stripe_sub_plan = None
    logger.info("Subscription canceled for org %s", org_id)


def handle_invoice_paid(invoice) -> None:
    """Handle successful invoice payment."""
    org_id = (
        invoice.subscription_details.metadata.get("org_id")
        if invoice.subscription_details
        else None
    )
    logger.info("Invoice paid: %s for org %s", invoice.id, org_id)


def handle_payment_failed(invoice) -> None:
    """Handle failed invoice payment — mark org as past_due."""
    org_id = (
        invoice.subscription_details.metadata.get("org_id")
        if invoice.subscription_details
        else None
    )
    if not org_id:
        logger.warning("Invoice %s has no org_id", invoice.id)
        return

    billing = Billing.query.filter_by(org_id=org_id).first()
    if not billing:
        return

    billing.stripe_sub_status = "past_due"
    logger.warning("Payment failed for org %s, marked as past_due", org_id)


def handle_invoice_created(invoice) -> None:
    """Handle invoice created — apply usage credits before it finalizes."""
    if not invoice.subscription:
        return

    sub = Subscription.retrieve(invoice.subscription)
    org_id = sub.metadata.get("org_id")
    plan_tier = sub.metadata.get("plan_tier")

    if not org_id or not plan_tier:
        return

    credit = PLAN_CREDITS.get(plan_tier, 0)
    if credit > 0:
        Customer.create_balance_transaction(
            invoice.customer,
            amount=-credit,
            currency="usd",
            description=f"{plan_tier} plan usage credit",
        )
        logger.info(
            "Applied %d cent credit for org %s (%s plan)", credit, org_id, plan_tier
        )


def handle_setup_intent_succeeded(setup_intent) -> None:
    """Set the confirmed payment method as the customer's default."""
    customer_id = setup_intent.customer
    payment_id = setup_intent.payment_method

    if customer_id and payment_id:
        Customer.modify(
            customer_id,
            invoice_settings={"default_payment_method": payment_id},
        )
        logger.info("Set default payment method %s for customer %s", payment_id, customer_id)
