"""Stripe module for stripe things."""

import logging
from flask_login import login_required
from stripe import checkout, Customer, Subscription, Webhook, SignatureVerificationError

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

# we need to build a stripe module to support usage and sub based payments.
# we want a payment intents or whatever so we can have the UI all in cabotage

# Things need to do:
# - create a stripe checkout session ✅
# - create(_or_get?) a stripe customer
# - create a stripe payment intent
# - create a stripe sub for those that do that


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
    """Handle incoming Stripe webhooks.

    Returns a tuple of (message, status code).
    """
    payload = request.get_data
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

    logger.info("Stripe webhook received and verfied: %s", event.type)
    # check if we already working on this
    _seen = BillingWebhookEvent.query.filter_by(stripe_event_id=event.id).first()
    if _seen:
        logger.info("Already working on this event, skipping")
        return "Already working on this event, skipping", 200

    webhook_event = BillingWebhookEvent(
        stripe_event_id=event.id, event_type=event.type, payload=event.data
    )
    db.session.add(webhook_event)

    # yay i get to use the 'new' match stmt
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

    db.session.commit()
    return "", 200


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


### --- Subscription stuffs
def create_sub(org: Organization, tier: PlanTier):
    """Create a Stripe subscription for payment."""
    customer = create_or_get_customer(org)
    _plan = PLANS[tier]

    products = [{"price": _plan.price_id}]
    for meter in METERS.values():
        products.append({"price": meter.price_id})

    sub = Subscription.create(
        customer=customer.id,
        items=products,
        payment_behavior="default_incomplete",  # we want a payment method
        metadata={"org_id": str(org.id), "org_slug": org.slug},
    )
    # no db update here, wait for our webhook to do it
    return sub


### --- Webhook handlers
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
    """Handle invoice created — apply usage credits before it finalizes.

    Stripe creates the invoice ~1 hour before finalizing,
    giving us a window to add the plan's usage credit.
    """
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
            amount=-credit,  # negative = credit
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
