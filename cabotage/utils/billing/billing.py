"""Stripe module for stripe things."""

import logging
from flask_login import login_required
from stripe import checkout, Customer, Subscription, Webhook, SignatureVerificationError

from flask import current_app, jsonify, Blueprint, Response, request

from cabotage.server import db
from cabotage.server.models import Organization
from cabotage.server.models.auth import Billing, BillingWebhookEvent
from cabotage.utils.billing._products import PLANS, PlanTier, METERS

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
        stripe_event_id=event.id, event_tyep=event.type, payload=event.data
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


def get_sub():
    """Retrieve a Stripe subscription for payment."""


def delete_sub():
    """Delete a Stripe subscription for payment."""
    pass


def cancel_sub():
    """Cancel a Stripe subscription for payment."""
    pass
