"""Stripe module for stripe things."""

import logging
from flask_login import login_required
from stripe import checkout, Customer

from flask import current_app, jsonify, Blueprint, Response

from cabotage.server import db
from cabotage.server.models import Organization
from cabotage.server.models.auth import Billing

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


def create_payment_intent():
    """Create a Stripe payment intent for payment."""
    pass


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
def create_sub():
    """Create a Stripe subscription for payment."""
    pass


def get_sub():
    """Retrieve a Stripe subscription for payment."""
    pass


def delete_sub():
    """Delete a Stripe subscription for payment."""
    pass


def cancel_sub():
    """Cancel a Stripe subscription for payment."""
    pass
