"""Stripe module for stripe things."""

import logging
import os
from typing import Final

from flask_login import login_required
from stripe import checkout

from flask import current_app, jsonify, Blueprint

logger = logging.getLogger(__name__)

STRIPE_PUB_KEY: Final[str] = os.getenv("STRIPE_PUB_KEY")
STRIPE_SECRET_KEY: Final[str] = os.getenv("STRIPE_SECRET_KEY")
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
def create_checkout_session():
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


def create_or_get_customer():
    """Create or retrieve a Stripe customer for payment."""
    pass


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
