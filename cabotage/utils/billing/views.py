"""Views for Stripe."""

from flask import render_template, request, jsonify, Response
from flask_login import login_required
from stripe import Customer, Subscription, SetupIntent

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
    invoices = []
    payment_method = None
    usage = []

    if org.billing and org.billing.stripe_customer_id:
        cust_id = org.billing.stripe_customer_id
        invoices = get_invoices(cust_id)
        payment_method = get_default_payment_method(cust_id)

        if org.billing.stripe_sub_id:
            usage = get_usage(cust_id, org.billing.stripe_sub_id)

    return render_template(
        "billing/dashboard.html",
        org=org,
        invoices=invoices,
        payment_method=payment_method,
        usage=usage,
    )


@stripe_blueprint.route("/<org_slug>/subscribe", methods=["GET", "POST"])
@login_required
def subscribe(org_slug: str) -> tuple[Response, int] | Response | str:
    """Plan selection + Stripe Payment Element for checkout."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    if request.method == "POST":
        data = request.get_json()
        plan_tier = data.get("plan")

        if plan_tier not in PLANS:
            return jsonify(error="Invalid plan"), 400

        # existing sub
        if org.billing and org.billing.stripe_sub_id:
            sub = Subscription.retrieve(org.billing.stripe_sub_id)
            if sub.status in ("active", "trialing"):
                plan = PLANS[plan_tier]
                Subscription.modify(
                    sub.id,
                    items=[{
                        "id": sub["items"].data[0].id,
                        "price": plan.price_id,
                    }],
                    metadata={"plan_tier": plan_tier},
                )
                return jsonify(redirect=f"/billing/{org_slug}/")

        # new sub
        sub = create_sub(org, plan_tier)
        client_secret = sub.latest_invoice.payment_intent.client_secret
        return jsonify(client_secret=client_secret)

    return render_template("billing/subscribe.html", org=org)


@stripe_blueprint.route("/<org_slug>/payment", methods=["GET", "POST"])
@login_required
def payment_methods(org_slug: str) -> Response | str:
    """Manage payment methods via Stripe Payment Element."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()

    if request.method == "POST":
        data = request.get_json() or {}
        customer = create_or_get_customer(org)

        if data.get("action") == "set_default":
            payment_method_id = data.get("payment_method")
            if payment_method_id:
                Customer.modify(
                    customer.id,
                    invoice_settings={"default_payment_method": payment_method_id},
                )
            return jsonify(ok=True)

        setup_intent = SetupIntent.create(
            customer=customer.id,
            payment_method_types=ALLOWED_PAYMENT_METHODS,
            metadata={"org_id": str(org.id)},
        )
        return jsonify(client_secret=setup_intent.client_secret)

    payment_method = None
    if org.billing and org.billing.stripe_customer_id:
        payment_method = get_default_payment_method(org.billing.stripe_customer_id)

    return render_template("billing/payment_methods.html", org=org, payment_method=payment_method)
