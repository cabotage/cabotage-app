"""Views for Stripe."""

from flask import render_template, request, jsonify, Response
from flask_login import login_required
from stripe import Subscription, SetupIntent

from cabotage.server.models import Organization
from cabotage.utils.billing._products import PLANS
from cabotage.utils.billing.core import (
    create_or_get_customer,
    create_sub,
    get_default_payment_method,
    get_invoices,
    get_usage,
    stripe_blueprint,
)

# dont allow cashapp and stuff
ALLOWED_PAYMENT_METHODS = ["card", "us_bank_account"]


@stripe_blueprint.route("/")
@login_required
def billing_index() -> str:
    """User-level billing overview — lists all orgs the user belongs to."""
    return render_template("billing/index.html")


@stripe_blueprint.route("/<org_slug>/", methods=["GET"])
@login_required
def dashboard(org_slug: str) -> str:
    """Render the billing dashboard."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    # TODO: fetch real usage/invoices/payment from Stripe API
    return render_template("billing/dashboard.html", org=org)


@stripe_blueprint.route("/<org_slug>/subscribe", methods=["GET", "POST"])
@login_required
def subscribe(org_slug: str) -> str:
    """Plan selection + Stripe Payment Element for checkout."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    return render_template("billing/subscribe.html", org=org)


@stripe_blueprint.route("/<org_slug>/payment", methods=["GET", "POST"])
@login_required
def payment_methods(org_slug: str) -> str:
    """Manage payment methods via Stripe Payment Element."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    return render_template("billing/payment_methods.html", org=org)
