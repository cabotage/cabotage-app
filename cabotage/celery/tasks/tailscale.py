import logging

import kubernetes
from celery import shared_task
from kubernetes.client.rest import ApiException

from flask import current_app

from cabotage.server import (
    db,
    kubernetes as kubernetes_ext,
)
from cabotage.server.models.auth import TailscaleIntegration

log = logging.getLogger(__name__)

CRD_GROUP = "cabotage.io"
CRD_VERSION = "v1"
CRD_PLURAL = "cabotagetailscaleoperatorconfigs"


def _operator_namespace(org):
    """The org-level namespace where the Tailscale operator lives."""
    return org.k8s_identifier


def _ensure_namespace(core_api, namespace):
    """Create the namespace if it doesn't exist."""
    try:
        core_api.read_namespace(namespace)
    except ApiException as exc:
        if exc.status == 404:
            core_api.create_namespace(
                kubernetes.client.V1Namespace(
                    metadata=kubernetes.client.V1ObjectMeta(name=namespace),
                )
            )
        else:
            raise


def _deploy_operator_config(org, integration):
    """Create/update the CabotageTailscaleOperatorConfig in the org namespace.

    The tailscale-operator-manager Kopf operator watches these and
    reconciles all the actual K8s resources (Deployment, RBAC, etc).
    """
    api_client = kubernetes_ext.kubernetes_client
    core_api = kubernetes.client.CoreV1Api(api_client)
    custom_api = kubernetes.client.CustomObjectsApi(api_client)

    namespace = _operator_namespace(org)
    _ensure_namespace(core_api, namespace)

    body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "CabotageTailscaleOperatorConfig",
        "metadata": {
            "name": org.k8s_identifier,
            "namespace": namespace,
        },
        "spec": {
            "clientId": integration.client_id,
            "defaultTags": integration.default_tags or "",
            "organizationSlug": org.slug,
        },
    }
    try:
        custom_api.get_namespaced_custom_object(
            CRD_GROUP,
            CRD_VERSION,
            namespace,
            CRD_PLURAL,
            org.k8s_identifier,
        )
        custom_api.patch_namespaced_custom_object(
            CRD_GROUP,
            CRD_VERSION,
            namespace,
            CRD_PLURAL,
            org.k8s_identifier,
            body,
        )
    except ApiException as exc:
        if exc.status == 404:
            custom_api.create_namespaced_custom_object(
                CRD_GROUP,
                CRD_VERSION,
                namespace,
                CRD_PLURAL,
                body,
            )
        else:
            raise


def _teardown_operator_config(org):
    """Delete the CabotageTailscaleOperatorConfig CRD resource.

    The tailscale-operator-manager handles cleanup via Kopf finalizers.
    """
    api_client = kubernetes_ext.kubernetes_client
    custom_api = kubernetes.client.CustomObjectsApi(api_client)
    namespace = _operator_namespace(org)

    try:
        custom_api.delete_namespaced_custom_object(
            CRD_GROUP,
            CRD_VERSION,
            namespace,
            CRD_PLURAL,
            org.k8s_identifier,
        )
    except ApiException as exc:
        if exc.status != 404:
            log.warning(
                "Failed to delete CabotageTailscaleOperatorConfig %s in %s: %s",
                org.k8s_identifier,
                namespace,
                exc,
            )


@shared_task()
def deploy_tailscale_operator(organization_id):
    """Create a CabotageTailscaleOperatorConfig for an organization."""
    integration = TailscaleIntegration.query.filter_by(
        organization_id=organization_id,
    ).first()
    if integration is None:
        log.warning("No TailscaleIntegration found for org %s", organization_id)
        return

    if not current_app.config.get("TAILSCALE_OPERATOR_ENABLED"):
        log.info("Tailscale operator disabled, skipping for org %s", organization_id)
        integration.operator_state = "disabled"
        db.session.commit()
        return

    org = integration.organization
    try:
        # Mint and write the first JWT before creating the CRD, so the
        # operator has a valid token from the moment the Tailnet CRD is created.
        from cabotage.utils.oidc import mint_tailscale_jwt

        namespace = _operator_namespace(org)
        operator_namespace = "tailscale"
        _ensure_namespace(
            kubernetes.client.CoreV1Api(kubernetes_ext.kubernetes_client),
            namespace,
        )
        jwt = mint_tailscale_jwt(org.k8s_identifier, integration.client_id)
        secret_name = f"tailscale-tailnet-{org.k8s_identifier}"
        core_api = kubernetes.client.CoreV1Api(kubernetes_ext.kubernetes_client)
        secret_body = kubernetes.client.V1Secret(
            metadata=kubernetes.client.V1ObjectMeta(
                name=secret_name,
                namespace=operator_namespace,
            ),
            string_data={
                "client_id": integration.client_id,
                "jwt": jwt,
            },
        )
        try:
            core_api.read_namespaced_secret(secret_name, operator_namespace)
            core_api.patch_namespaced_secret(
                secret_name, operator_namespace, secret_body
            )
        except ApiException as exc:
            if exc.status == 404:
                core_api.create_namespaced_secret(operator_namespace, secret_body)
            else:
                raise
        log.info("Wrote initial OIDC JWT for org %s", org.slug)

        _deploy_operator_config(org, integration)
        log.info(
            "Created CabotageTailscaleOperatorConfig in %s for org %s",
            namespace,
            org.slug,
        )
        integration.operator_state = "pending"
    except Exception:
        log.exception(
            "Failed to deploy Tailscale for org %s",
            org.slug,
        )
        integration.operator_state = "failed"

    db.session.commit()


@shared_task()
def teardown_tailscale_operator(organization_id):
    """Delete the CabotageTailscaleOperatorConfig for an organization."""
    integration = TailscaleIntegration.query.filter_by(
        organization_id=organization_id,
    ).first()
    if integration is None:
        log.warning("No TailscaleIntegration found for org %s", organization_id)
        return

    org = integration.organization
    integration.operator_state = "removing"
    db.session.commit()

    try:
        _teardown_operator_config(org)
        log.info(
            "Deleted CabotageTailscaleOperatorConfig for org %s",
            org.slug,
        )
        # Clean up the Tailnet credential Secret
        core_api = kubernetes.client.CoreV1Api(kubernetes_ext.kubernetes_client)
        secret_name = f"tailscale-tailnet-{org.k8s_identifier}"
        try:
            core_api.delete_namespaced_secret(secret_name, "tailscale")
        except ApiException as exc:
            if exc.status != 404:
                log.warning("Failed to delete Secret %s: %s", secret_name, exc)
        # K8s cleanup succeeded — remove the integration from DB
        db.session.delete(integration)
        db.session.commit()
    except Exception:
        log.exception(
            "Failed to delete CabotageTailscaleOperatorConfig for org %s",
            org.slug,
        )
        integration.operator_state = "failed"
        db.session.commit()


@shared_task()
def reconcile_tailscale_integration_states():
    """Periodic task: sync TailscaleIntegration.operator_state from CRD status."""
    integrations = TailscaleIntegration.query.filter(
        TailscaleIntegration.operator_state.in_(("pending", "deployed", "failed")),
    ).all()
    if not integrations:
        return

    api_client = kubernetes_ext.kubernetes_client
    custom_api = kubernetes.client.CustomObjectsApi(api_client)

    for integration in integrations:
        org = integration.organization
        namespace = _operator_namespace(org)
        try:
            crd = custom_api.get_namespaced_custom_object(
                CRD_GROUP,
                CRD_VERSION,
                namespace,
                CRD_PLURAL,
                org.k8s_identifier,
            )
        except ApiException as exc:
            if exc.status == 404 and integration.operator_state != "pending":
                integration.operator_state = "missing"
                db.session.commit()
            continue
        except (
            Exception
        ):  # nosec B112 — don't let one org's error stop the reconcile loop
            continue

        status = crd.get("status", {}).get("reconcile_operator", {})
        state = status.get("state")
        valid_states = {
            "pending",
            "deployed",
            "failed",
            "disabled",
            "missing",
            "removing",
        }
        if state and state in valid_states and state != integration.operator_state:
            integration.operator_state = state
            db.session.commit()
            log.info(
                "Updated operator state to %s for org %s",
                state,
                org.slug,
            )


@shared_task()
def refresh_tailscale_oidc_tokens():
    """Periodic task: mint fresh JWTs for all orgs with Tailscale integration."""
    from cabotage.utils.oidc import mint_tailscale_jwt

    integrations = TailscaleIntegration.query.filter(
        TailscaleIntegration.operator_state.in_(("pending", "deployed")),
    ).all()
    if not integrations:
        return

    api_client = kubernetes_ext.kubernetes_client
    core_api = kubernetes.client.CoreV1Api(api_client)

    # JWT Secrets live in the cabotage namespace (where the single operator reads them)
    operator_namespace = "tailscale"

    for integration in integrations:
        org = integration.organization
        secret_name = f"tailscale-tailnet-{org.k8s_identifier}"
        try:
            jwt = mint_tailscale_jwt(org.k8s_identifier, integration.client_id)
            secret_body = kubernetes.client.V1Secret(
                metadata=kubernetes.client.V1ObjectMeta(
                    name=secret_name,
                    namespace=operator_namespace,
                ),
                string_data={
                    "client_id": integration.client_id,
                    "jwt": jwt,
                },
            )
            try:
                core_api.read_namespaced_secret(secret_name, operator_namespace)
                core_api.patch_namespaced_secret(
                    secret_name, operator_namespace, secret_body
                )
            except ApiException as exc:
                if exc.status == 404:
                    core_api.create_namespaced_secret(operator_namespace, secret_body)
                else:
                    raise
            log.debug("Refreshed OIDC JWT for org %s", org.slug)
        except Exception:
            log.exception("Failed to refresh OIDC JWT for org %s", org.slug)
