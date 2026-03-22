"""Views for Stripe."""

import logging

from flask import render_template, request, jsonify, Response
from flask_login import login_required
from stripe import Customer, Subscription, SubscriptionItem, SetupIntent, PaymentMethod

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
from cabotage.utils.billing.metering import (
    collect_environment_usage,
    collect_project_usage,
    get_service_usage_list,
)

logger = logging.getLogger(__name__)

ALLOWED_PAYMENT_METHODS = ["card", "us_bank_account"]


@stripe_blueprint.route("/")
@login_required
def billing_index() -> str:
    """User-level billing overview — lists all orgs the user belongs to."""
    return render_template("billing/index.html")


@stripe_blueprint.route("/<org_slug>/", methods=["GET"])
@login_required
def dashboard(org_slug: str) -> str:
    """Render the billing dashboard — no Stripe calls, instant load."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    return render_template("billing/dashboard.html", org=org)


@stripe_blueprint.route("/<org_slug>/billing-invoices")
@login_required
def billing_invoices(org_slug: str) -> Response:
    """JSON endpoint for invoices."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    invoices = []

    if org.billing and org.billing.stripe_customer_id:
        try:
            invoices = get_invoices(org.billing.stripe_customer_id)
        except Exception:
            logger.exception("Failed to fetch invoices for %s", org.billing.stripe_customer_id)
    return jsonify(invoices=invoices)


@stripe_blueprint.route("/<org_slug>/billing-payment-method")
@login_required
def billing_payment_method(org_slug: str) -> Response:
    """JSON endpoint for payment method."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    payment_method = None

    if org.billing and org.billing.stripe_customer_id:
        try:
            payment_method = get_default_payment_method(org.billing.stripe_customer_id)
        except Exception:
            logger.exception("Failed to fetch payment method for %s", org.billing.stripe_customer_id)
    return jsonify(payment_method=payment_method)


@stripe_blueprint.route("/<org_slug>/billing-usage")
@login_required
def billing_usage(org_slug: str) -> Response:
    """JSON endpoint for usage data."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    usage = []

    if org.billing and org.billing.stripe_customer_id and org.billing.stripe_sub_id:
        try:
            usage = get_usage(org.billing.stripe_customer_id, org.billing.stripe_sub_id)
        except Exception:
            logger.exception("Failed to fetch usage for %s", org.billing.stripe_customer_id)
    return jsonify(usage=usage)


@stripe_blueprint.route("/<org_slug>/billing-service-usage")
@login_required
def billing_service_usage(org_slug: str) -> Response:
    """Per-service usage breakdown.

    Derived from pod_class × replicas for compute, mock data for other
    meters until K8s collectors are wired up.
    """
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    try:
        services = get_service_usage_list(org)
    except Exception:
        logger.exception("Failed to build service usage for org %s", org_slug)
        services = []
    return jsonify(services=services)


@stripe_blueprint.route("/<org_slug>/billing-project-usage/<project_slug>")
@login_required
def billing_project_usage(org_slug: str, project_slug: str) -> Response:
    """Per-service usage for a single project."""
    from cabotage.server.models.projects import Project
    from cabotage.utils.billing._products import METERS

    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=org.id, slug=project_slug,
    ).first_or_404()

    try:
        data = collect_project_usage(project)
        # Add cost to each service
        for svc in data["services"]:
            cost = 0.0
            for meter_key, value in svc["usage"].items():
                meter = METERS.get(meter_key)
                if meter and value > 0:
                    cost += value * float(meter.unit_amount_decimal) / 100
            svc["cost"] = round(cost, 2)
        # Total cost
        total_cost = sum(s["cost"] for s in data["services"])
        data["total_cost"] = round(total_cost, 2)
    except Exception:
        logger.exception("Failed to build project usage for %s/%s", org_slug, project_slug)
        data = {"project": project.name, "services": [], "totals": {}, "total_cost": 0}

    return jsonify(**data)


@stripe_blueprint.route("/<org_slug>/billing-env-usage/<project_slug>/<env_slug>")
@login_required
def billing_env_usage(org_slug: str, project_slug: str, env_slug: str) -> Response:
    """Per-service usage for a single environment."""
    from cabotage.server.models.projects import Environment, Project
    from cabotage.utils.billing._products import METERS

    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    project = Project.query.filter_by(
        organization_id=org.id, slug=project_slug,
    ).first_or_404()
    environment = Environment.query.filter_by(
        project_id=project.id, slug=env_slug,
    ).first_or_404()

    try:
        data = collect_environment_usage(environment)
        for svc in data["services"]:
            cost = 0.0
            for meter_key, value in svc["usage"].items():
                meter = METERS.get(meter_key)
                if meter and value > 0:
                    cost += value * float(meter.unit_amount_decimal) / 100
            svc["cost"] = round(cost, 2)
        data["total_cost"] = round(sum(s["cost"] for s in data["services"]), 2)
    except Exception:
        logger.exception("Failed to build env usage for %s/%s/%s", org_slug, project_slug, env_slug)
        data = {"environment": environment.name, "services": [], "totals": {}, "total_cost": 0}

    return jsonify(**data)


# ---------------------------------------------------------------------------
# Subscribe / plan management
# ---------------------------------------------------------------------------

def _switch_plan(org, plan_tier: str) -> Response:
    """Switch an existing active subscription to a new plan tier."""
    sub = Subscription.retrieve(org.billing.stripe_sub_id)
    if sub.status not in ("active", "trialing"):
        return jsonify(error="Subscription is not active."), 400

    new_plan = PLANS[plan_tier]
    plan_price_ids = {p.price_id for p in PLANS.values()}
    all_items = SubscriptionItem.list(subscription=sub.id, limit=100)
    plan_item = next(
        (item for item in all_items.data if item.price.id in plan_price_ids),
        None,
    )
    if not plan_item:
        logger.error(
            "Could not find plan item in subscription %s. Items: %s",
            sub.id, [i.price.id for i in all_items.data],
        )
        return jsonify(error="Could not find current plan in subscription."), 400

    # Require a payment method on file
    customer = Customer.retrieve(org.billing.stripe_customer_id)
    default_payment_method = (
        customer.invoice_settings.get("default_payment_method")
        if customer.invoice_settings else None
    )
    if not default_payment_method:
        return jsonify(error="Please add a payment method before switching plans."), 400
    modify_params = {
        "items": [{"id": plan_item.id, "price": new_plan.price_id}],
        "metadata": {"plan_tier": plan_tier},
        "proration_behavior": "always_invoice",
    }
    if default_payment_method:
        modify_params["default_payment_method"] = default_payment_method

    Subscription.modify(sub.id, **modify_params)
    return jsonify(redirect=f"/billing/{org.slug}/")


@stripe_blueprint.route("/<org_slug>/cancel", methods=["POST"])
@login_required
def cancel_subscription(org_slug: str) -> tuple[Response, int] | Response:
    """Cancel the org's subscription.

    Is not instant, rolls until the end of the billing period.
    """
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not org.billing or not org.billing.stripe_sub_id:
        return jsonify(error="No active subscription."), 400

    sub = Subscription.retrieve(org.billing.stripe_sub_id)
    if sub.status not in ("active", "trialing"):
        return jsonify(error="Subscription is not active."), 400

    Subscription.modify(sub.id, cancel_at_period_end=True)
    logger.info("Subscription %s set to cancel at period end for org %s", sub.id, org_slug)
    return jsonify(ok=True, redirect=f"/billing/{org_slug}/")


@stripe_blueprint.route("/<org_slug>/reactivate", methods=["POST"])
@login_required
def reactivate_subscription(org_slug: str) -> tuple[Response, int] | Response:
    """Undo a pending cancellation."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not org.billing or not org.billing.stripe_sub_id:
        return jsonify(error="No subscription."), 400

    customer = Customer.retrieve(org.billing.stripe_customer_id)
    has_pm = (
        customer.invoice_settings.get("default_payment_method")
        if customer.invoice_settings else None
    )
    if not has_pm:
        return jsonify(error="Please add a payment method before reactivating."), 400

    try:
        sub = Subscription.modify(org.billing.stripe_sub_id, cancel_at_period_end=False)
        logger.info("Reactivated subscription %s for org %s, status=%s, cancel_at_period_end=%s",
                     sub.id, org_slug, sub.status, sub.cancel_at_period_end)
    except Exception as e:
        logger.exception("Failed to reactivate subscription for org %s", org_slug)
        return jsonify(error=str(e)), 500
    return jsonify(ok=True, redirect=f"/billing/{org_slug}/")


@stripe_blueprint.route("/<org_slug>/remove-payment-method", methods=["POST"])
@login_required
def remove_payment_method(org_slug: str) -> tuple[Response, int] | Response:
    """Detach the default payment method from the customer."""
    org = Organization.query.filter_by(slug=org_slug).first_or_404()
    if not org.billing or not org.billing.stripe_customer_id:
        return jsonify(error="No billing record."), 400

    customer = Customer.retrieve(org.billing.stripe_customer_id)
    payment_method_id = (
        customer.invoice_settings.get("default_payment_method")
        if customer.invoice_settings else None
    )
    if not payment_method_id:
        return jsonify(error="No payment method on file."), 400

    # clear it first, then detach
    Customer.modify(
        customer.id,
        invoice_settings={"default_payment_method": ""},
    )
    PaymentMethod.detach(payment_method_id)
    logger.info("Detached payment method %s for org %s", payment_method_id, org_slug)
    return jsonify(ok=True)


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
            return _switch_plan(org, plan_tier)

        # New subscription
        sub = create_sub(org, plan_tier)
        if sub.latest_invoice and sub.latest_invoice.payment_intent:
            return jsonify(client_secret=sub.latest_invoice.payment_intent.client_secret)
        # Activated immediately (existing payment method on file)
        return jsonify(redirect=f"/billing/{org_slug}/")

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
    invoices = []
    if org.billing and org.billing.stripe_customer_id:
        payment_method = get_default_payment_method(org.billing.stripe_customer_id)
        invoices = get_invoices(org.billing.stripe_customer_id, limit=5)

    return render_template(
        "billing/payment_methods.html", org=org,
        payment_method=payment_method, invoices=invoices,
    )
