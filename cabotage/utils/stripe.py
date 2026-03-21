"""Stripe module for stripe things."""
import os
from typing import Final

STRIPE_PUB_KEY: Final[str] = os.getenv("STRIPE_PUB_KEY")
STRIPE_SECRET_KEY: Final[str] = os.getenv("STRIPE_SECRET_KEY")

# we need to build a stripe module to support usage and sub based payments.
# we want a payment intents or whatever so we can have the UI all in cabotage

# Things we need to do:
# - create(_or_get?) a stripe customer
# - create a stripe payment intent
# - create a stripe checkout session
# - create a stripe sub for those that do that

def create_checkout_session():
    """Create a Stripe checkout session for payment."""
    pass

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
