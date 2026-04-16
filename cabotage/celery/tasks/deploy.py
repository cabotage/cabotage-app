import logging
import secrets
import time

from base64 import b64encode

import kubernetes
import yaml

from celery import shared_task
from kubernetes.client.rest import ApiException
from sqlalchemy.orm.attributes import flag_modified

from flask import current_app

from cabotage.server import (
    config_writer,
    db,
    github_app,
    kubernetes as kubernetes_ext,
)

from cabotage.server.models.projects import (
    Configuration,
    Deployment,
    EnvironmentConfiguration,
    IngressHost,
    IngressSnapshot,
    DEFAULT_POD_CLASS,
    _ingress_hostname_pairs,
    pod_classes,
)

from cabotage.server.models.utils import (
    safe_k8s_name,
    compact_k8s_name,
    readable_k8s_hostname,
)

from cabotage.utils.build_log_stream import (
    _HEARTBEAT_TTL,
    get_redis_client,
    publish_end,
    publish_log_line,
    refresh_heartbeat,
    stream_key,
)
from cabotage.utils.github import (
    CheckRun,
    cabotage_url,
    post_deployment_status_update,
)
from cabotage.celery.tasks.notify import (
    dispatch_autodeploy_notification,
    dispatch_pipeline_notification,
)

log = logging.getLogger(__name__)


class DeployError(RuntimeError):
    pass


def _dispatch_deploy_failure(deployment, error_detail):
    try:
        app = deployment.application
        if deployment.deploy_metadata and deployment.deploy_metadata.get("auto_deploy"):
            image_id = deployment.deploy_metadata.get(
                "source_image_id", str(deployment.id)
            )
            dispatch_autodeploy_notification(
                "deploy_failed",
                image_id,
                app,
                deployment.application_environment,
                error=error_detail,
                image_url=cabotage_url(app, f"images/{image_id}"),
                deploy_url=cabotage_url(app, f"deployments/{deployment.id}"),
                image_metadata=deployment.deploy_metadata,
            )
        else:
            dispatch_pipeline_notification.delay(
                "pipeline.deploy",
                "Deployment",
                str(deployment.id),
                str(app.project.organization_id),
                str(app.id),
                str(deployment.application_environment_id)
                if deployment.application_environment_id
                else None,
                error=error_detail,
            )
    except Exception:
        log.warning("Failed to dispatch deploy failure notification", exc_info=True)


@shared_task()
def cleanup_app_env_k8s(app_env_id, namespace, resource_prefix, label_selector):
    """Best-effort delete of all k8s resources for one ApplicationEnvironment.

    All k8s addressing values are passed explicitly because the originating
    slugs may be renamed (``--deleted-…``) by the time the worker picks up
    the task, while the k8s resources still carry the original labels.
    """
    log = logging.getLogger(__name__)

    try:
        api_client = kubernetes_ext.kubernetes_client
        if api_client is None:
            log.warning("k8s cleanup: no kubernetes client available")
            return
    except Exception:
        log.exception("k8s cleanup: failed to get kubernetes client")
        return

    apps_api = kubernetes.client.AppsV1Api(api_client)
    core_api = kubernetes.client.CoreV1Api(api_client)
    networking_api = kubernetes.client.NetworkingV1Api(api_client)
    log.info("k8s cleanup: namespace=%s labels=%s", namespace, label_selector)

    # Delete Deployments
    try:
        deps = apps_api.list_namespaced_deployment(
            namespace, label_selector=label_selector
        )
        for d in deps.items:
            log.info(
                "k8s cleanup: deleting deployment %s/%s", namespace, d.metadata.name
            )
            apps_api.delete_namespaced_deployment(d.metadata.name, namespace)
    except Exception:
        log.exception("k8s cleanup: failed to delete deployments")

    # Delete Services
    svc_label_selector = f"resident-service.cabotage.io=true,app={resource_prefix}"
    try:
        svcs = core_api.list_namespaced_service(
            namespace, label_selector=svc_label_selector
        )
        for s in svcs.items:
            log.info("k8s cleanup: deleting service %s/%s", namespace, s.metadata.name)
            core_api.delete_namespaced_service(s.metadata.name, namespace)
    except Exception:
        log.exception("k8s cleanup: failed to delete services")

    # Delete Ingresses
    try:
        ings = networking_api.list_namespaced_ingress(
            namespace, label_selector=label_selector
        )
        for i in ings.items:
            log.info("k8s cleanup: deleting ingress %s/%s", namespace, i.metadata.name)
            networking_api.delete_namespaced_ingress(i.metadata.name, namespace)
    except Exception:
        log.exception("k8s cleanup: failed to delete ingresses")

    # Delete CabotageEnrollment
    try:
        custom_api = kubernetes.client.CustomObjectsApi(api_client)
        custom_api.delete_namespaced_custom_object(
            "cabotage.io", "v1", namespace, "cabotageenrollments", resource_prefix
        )
        log.info(
            "k8s cleanup: deleted CabotageEnrollment %s/%s", namespace, resource_prefix
        )
    except Exception:
        log.exception(
            "k8s cleanup: failed to delete CabotageEnrollment %s", resource_prefix
        )

    # Delete ServiceAccount and ImagePullSecret
    try:
        core_api.delete_namespaced_service_account(resource_prefix, namespace)
    except Exception:
        log.exception(
            "k8s cleanup: failed to delete service account %s", resource_prefix
        )
    try:
        core_api.delete_namespaced_secret(resource_prefix, namespace)
    except Exception:
        log.exception("k8s cleanup: failed to delete secret %s", resource_prefix)


def _preview_url_for_app_env(app_env):
    """Return the https:// URL for the first auto-generated ingress host, or None."""
    for ingress in app_env.ingresses:
        if not ingress.enabled:
            continue
        for host in ingress.hosts:
            if host.is_auto_generated and host.tls_enabled:
                return f"https://{host.hostname}"
    return None


def _retry_on_404(fn, *args, retries=5, delay=2, **kwargs):
    """Retry a kubernetes API call if it returns a 404, with backoff."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except ApiException as exc:
            if exc.status == 404 and attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                raise


def _wait_for_tls_certificate(api_client, namespace, cert_name, timeout=120, log=None):
    """Poll until a cert-manager Certificate is Ready, or timeout."""
    custom_api = kubernetes.client.CustomObjectsApi(api_client)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cert = custom_api.get_namespaced_custom_object(
                "cert-manager.io",
                "v1",
                namespace,
                "certificates",
                cert_name,
            )
            for cond in (cert.get("status") or {}).get("conditions", []):
                if cond.get("type") == "Ready" and cond.get("status") == "True":
                    if log:
                        log(f"Certificate {cert_name} is ready")
                    return True
        except ApiException as exc:
            if exc.status != 404:
                raise
        time.sleep(5)
    if log:
        log(f"Certificate {cert_name} not ready after {timeout}s, proceeding anyway")
    return False


def k8s_namespace(release):
    return release.application_environment.environment.k8s_namespace


def k8s_resource_prefix(release):
    return safe_k8s_name(
        release.application.project.k8s_identifier,
        release.application.k8s_identifier,
    )


def k8s_role_name(release):
    return f"{k8s_namespace(release)}-{k8s_resource_prefix(release)}"


def k8s_label_value(release):
    """Build a k8s label value (max 63 chars) for the 'app' label.

    Uses compact_k8s_name with (slug, k8s_identifier) pairs to produce
    a readable, unique value.  Legacy identifiers (slug == k8s_identifier)
    pass through unchanged; generated ones get their slugs joined with
    a combined hash suffix.
    """
    org = release.application.project.organization
    project = release.application.project
    app = release.application
    app_env = release.application_environment
    pairs = [(org.slug, org.k8s_identifier)]
    if app_env.environment.uses_environment_namespace:
        pairs.append((app_env.environment.slug, app_env.environment.k8s_identifier))
    pairs.append((project.slug, project.k8s_identifier))
    pairs.append((app.slug, app.k8s_identifier))
    return compact_k8s_name(*pairs)


def _safe_labels_from_release(release):
    """Build cabotage.io/-prefixed labels using k8s_identifiers.

    These are collision-safe labels that sit alongside the legacy
    slug-based labels.
    """
    org = release.application.project.organization
    project = release.application.project
    app = release.application
    app_env = release.application_environment
    labels = {
        "cabotage.io/organization": org.k8s_identifier,
        "cabotage.io/project": project.k8s_identifier,
        "cabotage.io/application": app.k8s_identifier,
    }
    if app_env.environment.uses_environment_namespace:
        labels["cabotage.io/environment"] = app_env.environment.k8s_identifier
    return labels


def _safe_labels_from_application(application):
    """Build cabotage.io/-prefixed labels from an Application (for builds)."""
    org = application.project.organization
    project = application.project
    return {
        "cabotage.io/organization": org.k8s_identifier,
        "cabotage.io/project": project.k8s_identifier,
        "cabotage.io/application": application.k8s_identifier,
    }


def render_namespace(release):
    namespace_object = kubernetes.client.V1Namespace(
        metadata=kubernetes.client.V1ObjectMeta(
            name=k8s_namespace(release),
            labels={
                "resident-namespace.cabotage.io": "true",
            },
        ),
    )
    return namespace_object


def create_namespace(core_api_instance, release):
    namespace_object = render_namespace(release)
    try:
        return core_api_instance.create_namespace(namespace_object)
    except Exception as exc:
        raise DeployError(
            "Unexpected exception creating Namespace/"
            f"{namespace_object.metadata.name}: {exc}"
        )


def ensure_namespace(core_api_instance, namespace_name):
    """Create the namespace if it doesn't exist, ensure resident label is set."""
    try:
        namespace = core_api_instance.read_namespace(namespace_name)
        # Ensure the resident-namespace label is present on existing namespaces
        labels = namespace.metadata.labels or {}
        if labels.get("resident-namespace.cabotage.io") != "true":
            namespace = core_api_instance.patch_namespace(
                namespace_name,
                kubernetes.client.V1Namespace(
                    metadata=kubernetes.client.V1ObjectMeta(
                        labels={"resident-namespace.cabotage.io": "true"},
                    ),
                ),
            )
    except ApiException as exc:
        if exc.status == 404:
            namespace = core_api_instance.create_namespace(
                kubernetes.client.V1Namespace(
                    metadata=kubernetes.client.V1ObjectMeta(
                        name=namespace_name,
                        labels={"resident-namespace.cabotage.io": "true"},
                    ),
                ),
            )
        else:
            raise DeployError(
                f"Unexpected exception fetching Namespace/{namespace_name}: {exc}"
            )
    return namespace


def fetch_namespace(core_api_instance, release):
    return ensure_namespace(core_api_instance, k8s_namespace(release))


TENANT_NETWORK_POLICIES = [
    {
        "name": "default-deny-ingress",
        "spec": {
            "podSelector": {},
            "policyTypes": ["Ingress"],
        },
    },
    # CNPG operator-managed pods need unrestricted egress to reach
    # the K8s API server for CRD reads at startup.
    {
        "name": "allow-egress-cnpg-pods",
        "spec": {
            "podSelector": {
                "matchExpressions": [
                    {"key": "cnpg.io/cluster", "operator": "Exists"},
                ],
            },
            "policyTypes": ["Egress"],
            "egress": [{}],
        },
    },
    # CNPG pods need ingress from the CNPG operator (status checks
    # on port 8000) and from other cluster members (replication on
    # port 5432).
    {
        "name": "allow-ingress-cnpg-pods",
        "spec": {
            "podSelector": {
                "matchExpressions": [
                    {"key": "cnpg.io/cluster", "operator": "Exists"},
                ],
            },
            "ingress": [
                # CNPG operator (runs in postgres namespace)
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "postgres",
                                },
                            },
                            "podSelector": {
                                "matchLabels": {
                                    "app.kubernetes.io/name": "cloudnative-pg",
                                },
                            },
                        },
                    ],
                    "ports": [
                        {"port": 5432, "protocol": "TCP"},
                        {"port": 8000, "protocol": "TCP"},
                    ],
                },
                # Intra-cluster replication between CNPG instances
                {
                    "from": [
                        {
                            "podSelector": {
                                "matchExpressions": [
                                    {
                                        "key": "cnpg.io/cluster",
                                        "operator": "Exists",
                                    },
                                ],
                            },
                        },
                    ],
                    "ports": [
                        {"port": 5432, "protocol": "TCP"},
                        {"port": 8000, "protocol": "TCP"},
                    ],
                },
            ],
        },
    },
    {
        "name": "allow-ingress-from-redis-operator",
        "spec": {
            "podSelector": {
                "matchLabels": {
                    "resident-redis.cabotage.io": "true",
                },
            },
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "redis",
                                },
                            },
                            "podSelector": {
                                "matchLabels": {
                                    "name": "redis-operator",
                                },
                            },
                        },
                    ],
                    "ports": [
                        {"port": 6379, "protocol": "TCP"},
                    ],
                },
            ],
        },
    },
    {
        "name": "allow-ingress-from-traefik",
        "spec": {
            "podSelector": {},
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "traefik",
                                },
                            },
                        },
                    ],
                    "ports": [
                        {"port": 8000, "protocol": "TCP"},
                        {"port": 8089, "protocol": "TCP"},
                    ],
                },
            ],
        },
    },
    {
        "name": "allow-ingress-from-tailscale",
        "spec": {
            "podSelector": {},
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "tailscale",
                                },
                            },
                            "podSelector": {
                                "matchLabels": {
                                    "tailscale.com/managed": "true",
                                },
                            },
                        },
                    ],
                    "ports": [{"port": 8000, "protocol": "TCP"}],
                },
            ],
        },
    },
    {
        "name": "allow-intra-namespace",
        "spec": {
            "podSelector": {},
            "ingress": [{"from": [{"podSelector": {}}]}],
        },
    },
    {
        "name": "restrict-egress",
        "spec": {
            "podSelector": {
                "matchExpressions": [
                    {
                        "key": "cnpg.io/cluster",
                        "operator": "DoesNotExist",
                    },
                ],
            },
            "policyTypes": ["Egress"],
            "egress": [
                # DNS resolution (kube-system CoreDNS)
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "kube-system",
                                },
                            },
                        },
                    ],
                    "ports": [
                        {"port": 53, "protocol": "UDP"},
                        {"port": 53, "protocol": "TCP"},
                    ],
                },
                # Vault — envconsul reads secrets at runtime
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "cabotage",
                                },
                            },
                            "podSelector": {
                                "matchLabels": {"app": "vault"},
                            },
                        },
                    ],
                    "ports": [
                        {"port": 443, "protocol": "TCP"},
                        {"port": 8200, "protocol": "TCP"},
                    ],
                },
                # Consul — envconsul reads config at runtime
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "cabotage",
                                },
                            },
                            "podSelector": {
                                "matchLabels": {"app": "consul"},
                            },
                        },
                    ],
                    "ports": [{"port": 8443, "protocol": "TCP"}],
                },
                # Legacy service providers
                # ClickHouse
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "clickhouse",
                                },
                            },
                        },
                    ],
                    "ports": [
                        {"port": 8443, "protocol": "TCP"},
                        {"port": 9440, "protocol": "TCP"},
                    ],
                },
                # Redis
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "redis",
                                },
                            },
                        },
                    ],
                    "ports": [{"port": 6379, "protocol": "TCP"}],
                },
                # Elasticsearch
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "elasticsearch",
                                },
                            },
                        },
                    ],
                    "ports": [{"port": 9200, "protocol": "TCP"}],
                },
                # PostgreSQL
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "postgres",
                                },
                            },
                        },
                    ],
                    "ports": [{"port": 5432, "protocol": "TCP"}],
                },
                # END Legacy service providers
                # Intra-namespace
                {"to": [{"podSelector": {}}]},
                # Internet — block cluster-internal CIDRs, allow external
                {
                    "to": [
                        {
                            "ipBlock": {
                                "cidr": "0.0.0.0/0",
                                "except": [
                                    "10.0.0.0/8",
                                    "100.64.0.0/10",
                                    "169.254.0.0/16",
                                    "172.16.0.0/12",
                                    "192.168.0.0/16",
                                ],
                            },
                        },
                    ],
                    "ports": [
                        {"protocol": "TCP"},
                        {"protocol": "UDP", "port": 123},
                        {"protocol": "UDP", "port": 443},
                        {"protocol": "UDP", "port": 8443},
                    ],
                },
            ],
        },
    },
]


def ensure_network_policies(networking_api, namespace):
    """Create or update tenant network policies in the given namespace."""
    for policy_def in TENANT_NETWORK_POLICIES:
        body = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": policy_def["name"],
                "namespace": namespace,
            },
            "spec": policy_def["spec"],
        }
        try:
            networking_api.read_namespaced_network_policy(policy_def["name"], namespace)
            networking_api.patch_namespaced_network_policy(
                policy_def["name"], namespace, body
            )
        except ApiException as exc:
            if exc.status == 404:
                networking_api.create_namespaced_network_policy(namespace, body)
            else:
                raise DeployError(
                    f"Unexpected exception ensuring NetworkPolicy/"
                    f"{policy_def['name']} in {namespace}: {exc}"
                )


def render_cabotage_ca_configmap(release):
    with open("/var/run/secrets/cabotage.io/ca.crt", "r") as f:
        ca_crt = f.read()
    configmap_object = kubernetes.client.V1ConfigMap(
        metadata=kubernetes.client.V1ObjectMeta(
            name="cabotage-ca",
        ),
        data={
            "ca.crt": ca_crt,
        },
    )
    return configmap_object


def create_cabotage_ca_configmap(core_api_instance, release):
    configmap_object = render_cabotage_ca_configmap(release)
    namespace_name = k8s_namespace(release)
    try:
        return core_api_instance.create_namespaced_config_map(
            namespace_name, configmap_object
        )
    except Exception as exc:
        raise DeployError(
            "Unexpected exception creating ConfigMap/cabotage-ca in "
            f"{namespace_name}: {exc}"
        )


def fetch_cabotage_ca_configmap(core_api_instance, release):
    namespace_name = k8s_namespace(release)
    try:
        configmap = core_api_instance.read_namespaced_config_map(
            "cabotage-ca", namespace_name
        )
    except ApiException as exc:
        if exc.status == 404:
            configmap = create_cabotage_ca_configmap(core_api_instance, release)
        else:
            raise DeployError(
                "Unexpected exception fetching ConfigMap/cabotage-ca in "
                f"{namespace_name}: {exc}"
            )
    return configmap


def render_service_account(release):
    service_account_name = k8s_resource_prefix(release)
    service_account_object = kubernetes.client.V1ServiceAccount(
        metadata=kubernetes.client.V1ObjectMeta(
            name=service_account_name,
        ),
    )
    return service_account_object


def create_service_account(core_api_instance, release):
    namespace = k8s_namespace(release)
    service_account_object = render_service_account(release)
    try:
        return core_api_instance.create_namespaced_service_account(
            namespace, service_account_object
        )
    except Exception as exc:
        raise DeployError(
            "Unexpected exception creating ServiceAccount/"
            f"{service_account_object.name} in {namespace}: {exc}"
        )


def fetch_service_account(core_api_instance, release):
    namespace = k8s_namespace(release)
    service_account_name = k8s_resource_prefix(release)
    try:
        service_account = core_api_instance.read_namespaced_service_account(
            service_account_name, namespace
        )
    except ApiException as exc:
        if exc.status == 404:
            service_account = create_service_account(core_api_instance, release)
        else:
            raise DeployError(
                "Unexpected exception fetching ServiceAccount/"
                f"{service_account_name} in {namespace}: {exc}"
            )
    return service_account


def render_cabotage_enrollment(release):
    cabotage_enrollment_name = k8s_resource_prefix(release)
    cabotage_enrollment_object = {
        "apiVersion": "cabotage.io/v1",
        "kind": "CabotageEnrollment",
        "metadata": {
            "name": cabotage_enrollment_name,
        },
    }
    spec = {}
    env = release.application_environment.environment
    forked_from = env.forked_from_environment
    if forked_from:
        org_k8s = release.application.project.organization.k8s_identifier
        base_ns = safe_k8s_name(org_k8s, forked_from.k8s_identifier)
        spec["inheritsFrom"] = [
            {
                "namespace": base_ns,
                "name": cabotage_enrollment_name,
            }
        ]
    read_keys = _compute_enrollment_read_keys(release)
    if read_keys:
        spec["readKeys"] = read_keys
    if spec:
        cabotage_enrollment_object["spec"] = spec
    return cabotage_enrollment_object


def create_cabotage_enrollment(custom_objects_api_instance, release):
    namespace = k8s_namespace(release)
    cabotage_enrollment_object = render_cabotage_enrollment(release)
    try:
        return custom_objects_api_instance.create_namespaced_custom_object(
            "cabotage.io",
            "v1",
            namespace,
            "cabotageenrollments",
            cabotage_enrollment_object,
        )
    except Exception as exc:
        raise DeployError(
            "Unexpected exception creating CabotageEnrollment/"
            f"{cabotage_enrollment_object.name} in {namespace}: {exc}"
        )


def fetch_cabotage_enrollment(custom_objects_api_instance, release):
    namespace = k8s_namespace(release)
    name = k8s_resource_prefix(release)
    try:
        cabotage_enrollment = custom_objects_api_instance.get_namespaced_custom_object(
            "cabotage.io", "v1", namespace, "cabotageenrollments", name
        )
    except ApiException as exc:
        if exc.status == 404:
            cabotage_enrollment = create_cabotage_enrollment(
                custom_objects_api_instance, release
            )
        else:
            raise DeployError(
                "Unexpected exception fetching CabotageEnrollment/"
                f"{name} in {namespace}: {exc}"
            )
    return cabotage_enrollment


def _env_config_read_keys_for_release(release):
    """Extract env config Consul/Vault paths from a release's configuration.

    Includes paths from:
    - Directly subscribed EnvironmentConfiguration objects
    - Shared secret refs (${shared.SECRET}) in app-level template configs
    """
    from cabotage.utils.config_templates import resolve_shared_secret_refs

    consul_keys = set()
    vault_keys = set()
    for name, config_data in release.configuration.items():
        obj = Configuration.query.get(config_data["id"])
        if obj is None:
            obj = EnvironmentConfiguration.query.get(config_data["id"])
        if isinstance(obj, EnvironmentConfiguration):
            if obj.key_slug:
                store, path = obj.key_slug.split(":", 1)
                prefix = "/".join(path.split("/")[:-1])
                if store == "consul":
                    consul_keys.add(f"{prefix}/")
                elif store == "vault":
                    vault_keys.add(f"{prefix}/*")
            if obj.secret and obj.buildtime and obj.build_key_slug:
                _, build_path = obj.build_key_slug.split(":", 1)
                build_prefix = "/".join(build_path.split("/")[:-1])
                vault_keys.add(f"{build_prefix}/*")
        elif isinstance(obj, Configuration) and obj.value:
            # Check for ${shared.*} refs that reference env-level configs
            secret_refs = resolve_shared_secret_refs(
                obj.value, release.application_environment
            )
            for _orig_name, env_cfg in secret_refs:
                if env_cfg.key_slug:
                    store, path = env_cfg.key_slug.split(":", 1)
                    prefix = "/".join(path.split("/")[:-1])
                    if store == "consul":
                        consul_keys.add(f"{prefix}/")
                    elif store == "vault":
                        vault_keys.add(f"{prefix}/*")
    return consul_keys, vault_keys


def _compute_enrollment_read_keys(release):
    """Compute the desired readKeys for a release's CabotageEnrollment.

    Returns the union of env config paths from the deploying release
    and the currently running release.
    """
    app_env = release.application_environment

    consul_keys = set()
    vault_keys = set()

    # Union: new release + currently deployed release
    c, v = _env_config_read_keys_for_release(release)
    consul_keys |= c
    vault_keys |= v

    current_deployment = app_env.latest_deployment_completed
    if current_deployment and current_deployment.release_object:
        c, v = _env_config_read_keys_for_release(current_deployment.release_object)
        consul_keys |= c
        vault_keys |= v

    desired = {}
    if consul_keys:
        desired["consul"] = sorted(consul_keys)
    if vault_keys:
        desired["vault"] = sorted(vault_keys)
    return desired


def reconcile_enrollment_read_keys(
    custom_objects_api_instance, enrollment, release, log=None
):
    """Patch the CabotageEnrollment's readKeys if they differ from what
    the deploying + current releases need."""
    desired = _compute_enrollment_read_keys(release)
    current = enrollment.get("spec", {}).get("readKeys", {})

    if desired == current:
        if log:
            log("CabotageEnrollment readKeys already current")
        return

    ns = k8s_namespace(release)
    name = k8s_resource_prefix(release)
    if log:
        log("Patching CabotageEnrollment readKeys")
    patch = {"spec": {"readKeys": desired}}
    custom_objects_api_instance.patch_namespaced_custom_object(
        "cabotage.io", "v1", ns, "cabotageenrollments", name, patch
    )


def render_service(release, process_name):
    resource_prefix = k8s_resource_prefix(release)
    service_name = f"{resource_prefix}-{process_name}"
    label_value = k8s_label_value(release)
    safe_labels = _safe_labels_from_release(release)
    service_object = kubernetes.client.V1Service(
        metadata=kubernetes.client.V1ObjectMeta(
            name=service_name,
            labels={
                "resident-service.cabotage.io": "true",
                "app": resource_prefix,
                "process": process_name,
                **safe_labels,
            },
        ),
        spec=kubernetes.client.V1ServiceSpec(
            ports=[
                kubernetes.client.V1ServicePort(
                    name="https",
                    port=8000,
                    target_port=8000,
                ),
            ],
            selector={
                "app": label_value,
                "process": process_name,
            },
        ),
    )
    return service_object


def create_service(core_api_instance, release, process_name):
    namespace = k8s_namespace(release)
    service_object = render_service(release, process_name)
    try:
        return core_api_instance.create_namespaced_service(namespace, service_object)
    except Exception as exc:
        raise DeployError(
            "Unexpected exception creating Service/"
            f"{service_object.metadata.name} in {namespace}: {exc}"
        )


def fetch_service(core_api_instance, release, process_name):
    namespace = k8s_namespace(release)
    service_object = render_service(release, process_name)
    service_name = service_object.metadata.name
    try:
        existing = core_api_instance.read_namespaced_service(service_name, namespace)
        # Patch the service to ensure ports and selectors stay current
        existing.spec.ports = service_object.spec.ports
        existing.spec.selector = service_object.spec.selector
        service = core_api_instance.patch_namespaced_service(
            service_name, namespace, existing
        )
    except ApiException as exc:
        if exc.status == 404:
            service = create_service(core_api_instance, release, process_name)
        else:
            raise DeployError(
                "Unexpected exception fetching Service/"
                f"{service_name} in {namespace}: {exc}"
            )
    return service


def render_ingress(release, ingress):
    """Build a V1Ingress from an Ingress model record.

    Convenience wrapper that extracts naming context from a Release.
    """
    safe_labels = _safe_labels_from_release(release)
    return render_ingress_object(
        ingress=ingress,
        resource_prefix=k8s_resource_prefix(release),
        labels={
            "organization": release.application.project.organization.slug,
            "project": release.application.project.slug,
            "application": release.application.slug,
            "app": k8s_label_value(release),
            **safe_labels,
        },
        org_k8s_identifier=release.application.project.organization.k8s_identifier,
        process_names=list(release.processes) if release.processes else [],
    )


def _build_ingress_paths(ingress, resource_prefix, process_names=None):
    """Build K8s path objects shared by both nginx and tailscale renderers."""
    port_spec = kubernetes.client.V1ServiceBackendPort(name="https")
    k8s_paths = []
    for path in ingress.paths:
        target_service = f"{resource_prefix}-{path.target_process_name}"
        k8s_paths.append(
            kubernetes.client.V1HTTPIngressPath(
                path=path.path,
                path_type=path.path_type,
                backend=kubernetes.client.V1IngressBackend(
                    service=kubernetes.client.V1IngressServiceBackend(
                        name=target_service,
                        port=port_spec,
                    ),
                ),
            )
        )

    if not k8s_paths:
        # No explicit paths — default to "/" routing to first web process
        web_procs = sorted(p for p in (process_names or []) if p.startswith("web"))
        if not web_procs:
            return None
        target_service = f"{resource_prefix}-{web_procs[0]}"
        k8s_paths.append(
            kubernetes.client.V1HTTPIngressPath(
                path="/",
                path_type="Prefix",
                backend=kubernetes.client.V1IngressBackend(
                    service=kubernetes.client.V1IngressServiceBackend(
                        name=target_service,
                        port=port_spec,
                    ),
                ),
            )
        )
    return k8s_paths


def render_ingress_object(
    ingress,
    resource_prefix,
    labels,
    org_k8s_identifier=None,
    org_default_tags=None,
    process_names=None,
):
    """Build a V1Ingress from an Ingress/IngressSnapshot and explicit context.

    This function has no dependency on a Release object, so it can be called
    during early ingress pre-creation (e.g. branch deploys) as well as during
    the normal deploy path.  Handles both nginx and tailscale ingress classes.
    """
    if not ingress.enabled:
        return None

    ingress_name = f"{resource_prefix}-{ingress.name}"
    is_tailscale = ingress.ingress_class_name == "tailscale"

    k8s_paths = _build_ingress_paths(ingress, resource_prefix, process_names)
    if k8s_paths is None:
        return None

    if is_tailscale:
        annotations = {}
        if org_k8s_identifier:
            annotations["tailscale.com/proxy-group"] = f"ingress-{org_k8s_identifier}"
        tags = ingress.tailscale_tags or org_default_tags
        if tags:
            annotations["tailscale.com/tags"] = tags
    else:
        annotations = {
            "nginx.ingress.kubernetes.io/backend-protocol": ingress.backend_protocol,
            "nginx.ingress.kubernetes.io/force-ssl-redirect": str(
                ingress.force_ssl_redirect
            ).lower(),
            "nginx.ingress.kubernetes.io/service-upstream": str(
                ingress.service_upstream
            ).lower(),
            "nginx.ingress.kubernetes.io/proxy-next-upstream": "error",
            "cert-manager.io/cluster-issuer": ingress.cluster_issuer,
        }
        annotations["nginx.ingress.kubernetes.io/proxy-connect-timeout"] = (
            ingress.proxy_connect_timeout or "10s"
        )
        annotations["nginx.ingress.kubernetes.io/proxy-read-timeout"] = (
            ingress.proxy_read_timeout or "10s"
        )
        annotations["nginx.ingress.kubernetes.io/proxy-send-timeout"] = (
            ingress.proxy_send_timeout or "10s"
        )
        annotations["nginx.ingress.kubernetes.io/proxy-body-size"] = (
            ingress.proxy_body_size or "10M"
        )
        annotations["nginx.ingress.kubernetes.io/client-body-buffer-size"] = (
            ingress.client_body_buffer_size or "1M"
        )
        annotations["nginx.ingress.kubernetes.io/proxy-request-buffering"] = (
            ingress.proxy_request_buffering or "on"
        )
        if ingress.session_affinity:
            annotations["nginx.ingress.kubernetes.io/affinity"] = "cookie"
        if ingress.use_regex:
            annotations["nginx.ingress.kubernetes.io/use-regex"] = "true"
        if ingress.allow_annotations and ingress.extra_annotations:
            annotations.update(ingress.extra_annotations)

    tls_hosts = []
    rules = []
    for host in ingress.hosts:
        if is_tailscale or host.tls_enabled:
            tls_hosts.append(host.hostname)
        rules.append(
            kubernetes.client.V1IngressRule(
                # Tailscale: don't set host on rules — the operator derives
                # it from tls.hosts and rejects mismatches with the FQDN
                host=None if is_tailscale else host.hostname,
                http=kubernetes.client.V1HTTPIngressRuleValue(paths=k8s_paths),
            )
        )

    tls = []
    if tls_hosts:
        tls_entry = kubernetes.client.V1IngressTLS(hosts=tls_hosts)
        if not is_tailscale:
            # nginx needs a secret name for cert-manager
            tls_entry.secret_name = f"{ingress_name}-tls"
        tls.append(tls_entry)

    all_labels = {"resident-ingress.cabotage.io": "true", "ingress": ingress.name}
    all_labels.update(labels)

    ingress_object = kubernetes.client.V1Ingress(
        metadata=kubernetes.client.V1ObjectMeta(
            name=ingress_name,
            labels=all_labels,
            annotations=annotations,
        ),
        spec=kubernetes.client.V1IngressSpec(
            ingress_class_name=ingress.ingress_class_name,
            rules=rules,
            tls=tls if tls else None,
        ),
    )
    return ingress_object


def create_ingress(networking_api, release, ingress):
    namespace = k8s_namespace(release)
    ingress_object = render_ingress(release, ingress)
    if ingress_object is None:
        return None
    try:
        return networking_api.create_namespaced_ingress(namespace, ingress_object)
    except Exception as exc:
        raise DeployError(
            "Unexpected exception creating Ingress/"
            f"{ingress_object.metadata.name} in {namespace}: {exc}"
        )


def delete_ingress(networking_api, release, ingress_name):
    namespace = k8s_namespace(release)
    k8s_name = f"{k8s_resource_prefix(release)}-{ingress_name}"
    try:
        networking_api.delete_namespaced_ingress(k8s_name, namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise DeployError(
                "Unexpected exception deleting Ingress/"
                f"{k8s_name} in {namespace}: {exc}"
            )


def fetch_ingress(networking_api, release, ingress):
    namespace = k8s_namespace(release)
    k8s_name = f"{k8s_resource_prefix(release)}-{ingress.name}"

    if not ingress.enabled:
        # Ingress disabled — delete if exists
        delete_ingress(networking_api, release, ingress.name)
        return None

    ingress_object = render_ingress(release, ingress)
    if ingress_object is None:
        # Enabled but no paths/hosts — skip without deleting
        return None

    try:
        networking_api.read_namespaced_ingress(k8s_name, namespace)
        return networking_api.patch_namespaced_ingress(
            k8s_name, namespace, ingress_object
        )
    except ApiException as exc:
        if exc.status == 404:
            return create_ingress(networking_api, release, ingress)
        raise DeployError(
            f"Unexpected exception fetching Ingress/{k8s_name} in {namespace}: {exc}"
        )


def ensure_ingresses(
    networking_api,
    namespace,
    resource_prefix,
    labels,
    ingresses,
    org_k8s_identifier=None,
    org_default_tags=None,
    process_names=None,
    cleanup_orphans=False,
    log=None,
):
    """Create or update K8s Ingress resources and optionally remove orphans.

    This is the single entry point for ingress management, used by both the
    normal deploy path and the branch-deploy pre-creation path.
    """
    active_names = []
    for ingress in ingresses:
        k8s_name = f"{resource_prefix}-{ingress.name}"

        if not ingress.enabled:
            # Disabled — delete if exists
            try:
                networking_api.delete_namespaced_ingress(k8s_name, namespace)
            except ApiException as exc:
                if exc.status != 404:
                    if log:
                        log(f"Failed to delete disabled Ingress/{k8s_name}: {exc}")
            continue

        ingress_object = render_ingress_object(
            ingress=ingress,
            resource_prefix=resource_prefix,
            labels=labels,
            org_k8s_identifier=org_k8s_identifier,
            org_default_tags=org_default_tags,
            process_names=process_names,
        )
        if ingress_object is None:
            continue

        active_names.append(ingress.name)
        if log:
            log(f"Ensuring Ingress/{ingress.name}")

        try:
            networking_api.read_namespaced_ingress(k8s_name, namespace)
            networking_api.patch_namespaced_ingress(k8s_name, namespace, ingress_object)
        except ApiException as exc:
            if exc.status == 404:
                networking_api.create_namespaced_ingress(namespace, ingress_object)
            else:
                raise DeployError(
                    f"Unexpected exception ensuring Ingress/{k8s_name} "
                    f"in {namespace}: {exc}"
                )

    if cleanup_orphans and active_names:
        _cleanup_orphaned_ingresses(
            networking_api, namespace, resource_prefix, labels, active_names, log=log
        )


def _cleanup_orphaned_ingresses(
    networking_api, namespace, resource_prefix, labels, active_ingress_names, log=None
):
    """Delete cabotage-managed ingresses that are no longer in the app's ingress list."""
    label_selector = ",".join(
        f"{k}={v}"
        for k, v in labels.items()
        if k in ("organization", "project", "application")
    )
    label_selector += ",resident-ingress.cabotage.io=true"
    try:
        existing = networking_api.list_namespaced_ingress(
            namespace, label_selector=label_selector
        )
    except ApiException:
        return
    active_k8s_names = {f"{resource_prefix}-{n}" for n in active_ingress_names}
    for item in existing.items:
        if item.metadata.name not in active_k8s_names:
            if log:
                log(f"Deleting orphaned Ingress/{item.metadata.name}")
            try:
                networking_api.delete_namespaced_ingress(item.metadata.name, namespace)
            except ApiException as exc:
                if exc.status != 404:
                    if log:
                        log(
                            f"Failed to delete orphaned Ingress/"
                            f"{item.metadata.name}: {exc}"
                        )


def cleanup_orphaned_ingresses(networking_api, release, active_ingress_names, log=None):
    """Delete cabotage-managed ingresses that are no longer in the app's ingress list."""
    namespace = k8s_namespace(release)
    resource_prefix = k8s_resource_prefix(release)
    label_selector = ",".join(
        [
            "resident-ingress.cabotage.io=true",
            f"organization={release.application.project.organization.slug}",
            f"project={release.application.project.slug}",
            f"application={release.application.slug}",
        ]
    )
    try:
        existing = networking_api.list_namespaced_ingress(
            namespace, label_selector=label_selector
        )
    except ApiException:
        return
    expected_names = {f"{resource_prefix}-{name}" for name in active_ingress_names}
    for item in existing.items:
        if item.metadata.name not in expected_names:
            if log:
                log(f"Deleting orphaned Ingress/{item.metadata.name}")
            try:
                networking_api.delete_namespaced_ingress(item.metadata.name, namespace)
            except ApiException as exc:
                if exc.status != 404:
                    if log:
                        log(
                            f"Warning: failed to delete orphaned Ingress/{item.metadata.name}: {exc}"
                        )


def cleanup_orphaned_deployments_and_services(
    apps_api, core_api, release, active_process_names, log=None
):
    """Delete k8s Deployments and Services for processes no longer in the Procfile."""
    namespace = k8s_namespace(release)
    resource_prefix = k8s_resource_prefix(release)
    label_selector = ",".join(
        [
            "resident-deployment.cabotage.io=true",
            f"organization={release.application.project.organization.slug}",
            f"project={release.application.project.slug}",
            f"application={release.application.slug}",
        ]
    )
    expected_names = {f"{resource_prefix}-{name}" for name in active_process_names}

    # Clean up Deployments
    try:
        existing = apps_api.list_namespaced_deployment(
            namespace, label_selector=label_selector
        )
    except ApiException:
        existing = None
    for item in existing.items if existing else []:
        if item.metadata.name not in expected_names:
            if log:
                log(f"Deleting orphaned Deployment/{item.metadata.name}")
            try:
                apps_api.delete_namespaced_deployment(item.metadata.name, namespace)
            except ApiException as exc:
                if exc.status != 404:
                    if log:
                        log(
                            f"Warning: failed to delete orphaned Deployment/{item.metadata.name}: {exc}"
                        )

    # Clean up Services (services use app=<resource_prefix> label, not org/project/application)
    svc_label_selector = f"resident-service.cabotage.io=true,app={resource_prefix}"
    expected_svc_names = expected_names  # same naming convention
    try:
        existing_svcs = core_api.list_namespaced_service(
            namespace, label_selector=svc_label_selector
        )
    except ApiException:
        return
    for item in existing_svcs.items:
        if item.metadata.name not in expected_svc_names:
            if log:
                log(f"Deleting orphaned Service/{item.metadata.name}")
            try:
                core_api.delete_namespaced_service(item.metadata.name, namespace)
            except ApiException as exc:
                if exc.status != 404:
                    if log:
                        log(
                            f"Warning: failed to delete orphaned Service/{item.metadata.name}: {exc}"
                        )


def cleanup_orphaned_cronjobs(batch_api, release, active_job_names, log=None):
    """Delete k8s CronJobs for job processes no longer in the Procfile."""
    namespace = k8s_namespace(release)
    resource_prefix = k8s_resource_prefix(release)
    label_selector = ",".join(
        [
            "resident-cronjob.cabotage.io=true",
            f"organization={release.application.project.organization.slug}",
            f"project={release.application.project.slug}",
            f"application={release.application.slug}",
        ]
    )
    expected_names = {f"{resource_prefix}-{name}" for name in active_job_names}

    try:
        existing = batch_api.list_namespaced_cron_job(
            namespace, label_selector=label_selector
        )
    except ApiException:
        return
    for item in existing.items if existing else []:
        if item.metadata.name not in expected_names:
            if log:
                log(f"Deleting orphaned CronJob/{item.metadata.name}")
            try:
                batch_api.delete_namespaced_cron_job(item.metadata.name, namespace)
            except ApiException as exc:
                if exc.status != 404:
                    if log:
                        log(
                            f"Warning: failed to delete orphaned CronJob/{item.metadata.name}: {exc}"
                        )


def render_image_pull_secrets(release):
    registry_auth_secret = current_app.config["REGISTRY_AUTH_SECRET"]
    secret = kubernetes.client.V1Secret(
        type="kubernetes.io/dockerconfigjson",
        metadata=kubernetes.client.V1ObjectMeta(
            name=k8s_resource_prefix(release),
        ),
        data={
            ".dockerconfigjson": b64encode(
                release.image_pull_secrets(
                    registry_auth_secret,
                    registry_urls=[current_app.config["REGISTRY_PULL"]],
                ).encode()
            ).decode(),
        },
    )
    return secret


def create_image_pull_secret(core_api_instance, release):
    namespace = k8s_namespace(release)
    secret_name = k8s_resource_prefix(release)
    image_pull_secrets = render_image_pull_secrets(release)
    try:
        return core_api_instance.create_namespaced_secret(namespace, image_pull_secrets)
    except Exception as exc:
        raise DeployError(
            f"Unexpected exception creating Secret/{secret_name} in {namespace}: {exc}"
        )


def fetch_image_pull_secrets(core_api_instance, release):
    namespace = k8s_namespace(release)
    secret_name = k8s_resource_prefix(release)
    try:
        secret = core_api_instance.read_namespaced_secret(secret_name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            secret = create_image_pull_secret(core_api_instance, release)
        else:
            raise DeployError(
                "Unexpected exception fetching ServiceAccount/"
                f"{secret_name} in {namespace}: {exc}"
            )
    return secret


def render_cabotage_sidecar_container(release, process_name, with_tls=True):
    role_name = k8s_role_name(release)
    resource_prefix = k8s_resource_prefix(release)

    args = [
        "kube-login-and-maintain",
        "--namespace=$(NAMESPACE)",
        f"--vault-auth-kubernetes-role={role_name}",
        "--fetch-consul-token",
        f"--vault-consul-role={role_name}",
        "--pod-name=$(POD_NAME)",
        "--pod-ip=$(POD_IP)",
    ]

    if with_tls:
        args.append("--fetch-cert")
        args.append(f"--vault-pki-role={role_name}")
        args.append(f"--service-names={resource_prefix}-{process_name}")

    return kubernetes.client.V1Container(
        name="cabotage-sidecar",
        restart_policy="Always",
        image=current_app.config["SIDECAR_IMAGE"],
        image_pull_policy="IfNotPresent",
        env=[
            kubernetes.client.V1EnvVar(
                name="NAMESPACE",
                value_from=kubernetes.client.V1EnvVarSource(
                    field_ref=kubernetes.client.V1ObjectFieldSelector(
                        field_path="metadata.namespace"
                    )
                ),
            ),
            kubernetes.client.V1EnvVar(
                name="POD_NAME",
                value_from=kubernetes.client.V1EnvVarSource(
                    field_ref=kubernetes.client.V1ObjectFieldSelector(
                        field_path="metadata.name"
                    )
                ),
            ),
            kubernetes.client.V1EnvVar(
                name="POD_IP",
                value_from=kubernetes.client.V1EnvVarSource(
                    field_ref=kubernetes.client.V1ObjectFieldSelector(
                        field_path="status.podIP"
                    )
                ),
            ),
        ],
        args=args,
        startup_probe=kubernetes.client.V1Probe(
            _exec=kubernetes.client.V1ExecAction(
                command=[
                    "sh",
                    "-c",
                    "test -f /var/run/secrets/vault/vault-token && "
                    "test -f /var/run/secrets/vault/consul-token",
                ],
            ),
            period_seconds=1,
            failure_threshold=30,
        ),
        volume_mounts=[
            kubernetes.client.V1VolumeMount(
                name="vault-secrets", mount_path="/var/run/secrets/vault"
            ),
        ],
        resources=kubernetes.client.V1ResourceRequirements(
            limits={
                "memory": "48Mi",
                "cpu": "50m",
            },
            requests={
                "memory": "32Mi",
                "cpu": "10m",
            },
        ),
    )


def render_cabotage_sidecar_tls_container(release, unix=True, tcp=False):
    volume_mounts = [
        kubernetes.client.V1VolumeMount(
            name="vault-secrets", mount_path="/var/run/secrets/vault"
        )
    ]
    if unix:
        volume_mounts.append(
            kubernetes.client.V1VolumeMount(
                name="cabotage-sock", mount_path="/var/run/cabotage"
            )
        )
        target = "unix:///var/run/cabotage/cabotage.sock"
    else:
        target = "127.0.0.1:8001"
    if tcp:
        liveness_probe = kubernetes.client.V1Probe(
            tcp_socket=kubernetes.client.V1TCPSocketAction(
                port=8000,
            ),
            initial_delay_seconds=10,
            period_seconds=3,
            timeout_seconds=2,
        )
        readiness_probe = kubernetes.client.V1Probe(
            tcp_socket=kubernetes.client.V1TCPSocketAction(
                port=8000,
            ),
            initial_delay_seconds=10,
            period_seconds=3,
            timeout_seconds=2,
        )
    else:
        liveness_probe = kubernetes.client.V1Probe(
            http_get=kubernetes.client.V1HTTPGetAction(
                scheme="HTTPS",
                port=8000,
                http_headers=(
                    [
                        kubernetes.client.V1HTTPHeader(
                            name="Host", value=release.health_check_host
                        )
                    ]
                    if release.health_check_host
                    else None
                ),
                path=release.health_check_path,
            ),
            initial_delay_seconds=10,
            period_seconds=3,
            timeout_seconds=2,
        )
        readiness_probe = kubernetes.client.V1Probe(
            http_get=kubernetes.client.V1HTTPGetAction(
                scheme="HTTPS",
                port=8000,
                http_headers=(
                    [
                        kubernetes.client.V1HTTPHeader(
                            name="Host", value=release.health_check_host
                        )
                    ]
                    if release.health_check_host
                    else None
                ),
                path=release.health_check_path,
            ),
            initial_delay_seconds=10,
            period_seconds=3,
            timeout_seconds=2,
        )
    return kubernetes.client.V1Container(
        name="cabotage-sidecar-tls",
        restart_policy="Always",
        image=current_app.config["SIDECAR_IMAGE"],
        image_pull_policy="IfNotPresent",
        command=["/usr/bin/ghostunnel"],
        args=[
            "server",
            "--keystore=/var/run/secrets/vault/combined.pem",
            "--cacert=/var/run/secrets/cabotage.io/ca.crt",
            "--timed-reload=300s",
            "--shutdown-timeout=10s",
            "--connect-timeout=10s",
            "--disable-authentication",
            "--quiet=handshake-errs",
            "--quiet=conns",
            f"--target={target}",
            "--listen=0.0.0.0:8000",
        ],
        volume_mounts=volume_mounts,
        ports=[
            kubernetes.client.V1ContainerPort(
                protocol="TCP",
                name="tls",
                container_port=8000,
            ),
        ],
        liveness_probe=liveness_probe,
        readiness_probe=readiness_probe,
        resources=kubernetes.client.V1ResourceRequirements(
            limits={
                "memory": "128Mi",
                "cpu": "100m",
            },
            requests={
                "memory": "64Mi",
                "cpu": "20m",
            },
        ),
    )


def render_process_container(
    release, process_name, datadog_tags, with_tls=True, unix=True
):
    volume_mounts = [
        kubernetes.client.V1VolumeMount(
            name="vault-secrets", mount_path="/var/run/secrets/vault"
        ),
    ]
    if unix:
        volume_mounts.append(
            kubernetes.client.V1VolumeMount(
                name="cabotage-sock", mount_path="/var/run/cabotage"
            )
        )
    app_env = release.application_environment
    process_pod_cls = (app_env.process_pod_classes or {}).get(
        process_name, DEFAULT_POD_CLASS
    )
    pod_class = pod_classes[process_pod_cls]
    return kubernetes.client.V1Container(
        name=process_name,
        image=f"{current_app.config['REGISTRY_PULL']}/{release.repository_name}:release-{release.version}",
        image_pull_policy="Always",
        env=[
            kubernetes.client.V1EnvVar(
                name="VAULT_ADDR", value="https://vault.cabotage.svc.cluster.local"
            ),
            kubernetes.client.V1EnvVar(
                name="VAULT_CACERT", value="/var/run/secrets/cabotage.io/ca.crt"
            ),
            kubernetes.client.V1EnvVar(
                name="CONSUL_HTTP_ADDR",
                value="https://consul.cabotage.svc.cluster.local:8443",
            ),
            kubernetes.client.V1EnvVar(
                name="CONSUL_CACERT", value="/var/run/secrets/cabotage.io/ca.crt"
            ),
            kubernetes.client.V1EnvVar(
                name="DATADOG_TAGS",
                value=",".join([f"{k}:{v}" for k, v in datadog_tags.items()]),
            ),
            kubernetes.client.V1EnvVar(name="SOURCE_COMMIT", value=release.commit_sha),
        ],
        args=[
            "envconsul",
            "-kill-signal=SIGTERM",
            f"-config=/etc/cabotage/envconsul-{process_name}.hcl",
        ],
        resources=kubernetes.client.V1ResourceRequirements(
            limits={
                "memory": pod_class["memory"]["limits"],
                "cpu": pod_class["cpu"]["limits"],
            },
            requests={
                "memory": pod_class["memory"]["requests"],
                "cpu": pod_class["cpu"]["requests"],
            },
        ),
        volume_mounts=volume_mounts,
    )


def render_datadog_container(dd_api_key, datadog_tags):
    return kubernetes.client.V1Container(
        name="dogstatsd-sidecar",
        restart_policy="Always",
        image=current_app.config["DATADOG_IMAGE"],
        image_pull_policy="IfNotPresent",
        env=[
            kubernetes.client.V1EnvVar(name="DD_API_KEY", value=dd_api_key),
            kubernetes.client.V1EnvVar(
                name="DD_HOSTNAME",
                value_from=kubernetes.client.V1EnvVarSource(
                    field_ref=kubernetes.client.V1ObjectFieldSelector(
                        api_version="v1", field_path="metadata.name"
                    )
                ),
            ),
            kubernetes.client.V1EnvVar(
                name="DD_DOGSTATSD_TAGS",
                value=" ".join([f"{k}:{v}" for k, v in datadog_tags.items()]),
            ),
            kubernetes.client.V1EnvVar(
                name="DD_TAGS",
                value=" ".join([f"{k}:{v}" for k, v in datadog_tags.items()]),
            ),
            kubernetes.client.V1EnvVar(name="DD_USE_DOGSTATSD", value="true"),
            kubernetes.client.V1EnvVar(name="DD_APM_ENABLED", value="true"),
            kubernetes.client.V1EnvVar(name="DD_LOGS_ENABLED", value="false"),
            kubernetes.client.V1EnvVar(
                name="DD_CONFD_PATH",
                value="/tmp/null",  # nosec
            ),
            kubernetes.client.V1EnvVar(
                name="DD_AUTOCONF_TEMPLATE_DIR",
                value="/tmp/null",  # nosec
            ),
            kubernetes.client.V1EnvVar(name="DD_ENABLE_GOHAI", value="false"),
            kubernetes.client.V1EnvVar(
                name="DD_COLLECT_KUBERNETES_EVENTS", value="false"
            ),
            kubernetes.client.V1EnvVar(
                name="DD_ENABLE_METADATA_COLLECTION", value="false"
            ),
            kubernetes.client.V1EnvVar(name="DD_ENABLE_PAYLOADS_EVENTS", value="true"),
            kubernetes.client.V1EnvVar(name="DD_ENABLE_PAYLOADS_SERIES", value="true"),
            kubernetes.client.V1EnvVar(
                name="DD_ENABLE_PAYLOADS_SERVICE_CHECKS", value="false"
            ),
            kubernetes.client.V1EnvVar(
                name="DD_ENABLE_PAYLOADS_SKETCHES", value="false"
            ),
            kubernetes.client.V1EnvVar(
                name="DD_PROCESS_CONFIG_PROCESS_COLLECTION_ENABLED", value="false"
            ),
            kubernetes.client.V1EnvVar(
                name="DD_AUTOCONFIG_EXCLUDE_FEATURES",
                value="cloudfoundry cri docker ecsec2 ecsfargate eksfargate kubernetes orchestratorexplorer podman",
            ),
        ],
        resources=kubernetes.client.V1ResourceRequirements(
            limits={
                "memory": "256Mi",
                "cpu": "50m",
            },
            requests={
                "memory": "192Mi",
                "cpu": "25m",
            },
        ),
    )


def render_podspec(release, process_name, service_account_name):
    datadog_tags = {
        "organization": release.application.project.organization.slug,
        "project": release.application.project.slug,
        "application": release.application.slug,
        "process": process_name,
        "app": k8s_label_value(release),
        "release": str(release.version),
    }
    volumes = [
        kubernetes.client.V1Volume(
            name="vault-secrets",
            empty_dir=kubernetes.client.V1EmptyDirVolumeSource(
                medium="Memory", size_limit="1M"
            ),
        ),
    ]
    init_containers = []
    containers = []
    restart_policy = None

    if process_name.startswith("web"):
        volumes.append(
            kubernetes.client.V1Volume(
                name="cabotage-sock",
                empty_dir=kubernetes.client.V1EmptyDirVolumeSource(
                    medium="Memory", size_limit="1M"
                ),
            )
        )
        init_containers.append(
            render_cabotage_sidecar_container(release, process_name, with_tls=True)
        )
        init_containers.append(
            render_cabotage_sidecar_tls_container(release, unix=True)
        )
        containers.append(
            render_process_container(
                release, process_name, datadog_tags, with_tls=True, unix=True
            )
        )
    elif process_name.startswith("tcp"):
        init_containers.append(
            render_cabotage_sidecar_container(release, process_name, with_tls=True)
        )
        init_containers.append(
            render_cabotage_sidecar_tls_container(release, unix=False, tcp=True)
        )
        containers.append(
            render_process_container(
                release, process_name, datadog_tags, with_tls=True, unix=False
            )
        )
    elif process_name.startswith("worker"):
        init_containers.append(
            render_cabotage_sidecar_container(release, process_name, with_tls=False)
        )
        containers.append(
            render_process_container(
                release, process_name, datadog_tags, with_tls=False, unix=False
            )
        )
    elif process_name.startswith("job"):
        init_containers.append(
            render_cabotage_sidecar_container(release, process_name, with_tls=False)
        )
        containers.append(
            render_process_container(
                release, process_name, datadog_tags, with_tls=False, unix=False
            )
        )
        restart_policy = "OnFailure"
    elif process_name.startswith("release"):
        init_containers.append(
            render_cabotage_sidecar_container(release, process_name, with_tls=False)
        )
        containers.append(
            render_process_container(
                release, process_name, datadog_tags, with_tls=False, unix=False
            )
        )
        restart_policy = "Never"
    elif process_name.startswith("postdeploy"):
        init_containers.append(
            render_cabotage_sidecar_container(release, process_name, with_tls=False)
        )
        containers.append(
            render_process_container(
                release, process_name, datadog_tags, with_tls=False, unix=False
            )
        )
        restart_policy = "Never"
    else:
        init_containers.append(
            render_cabotage_sidecar_container(release, process_name, with_tls=False)
        )
        containers.append(
            render_process_container(
                release, process_name, datadog_tags, with_tls=False
            )
        )

    if (
        not (
            process_name.startswith("release") or process_name.startswith("postdeploy")
        )
        and "DD_API_KEY" in release.configuration_objects
    ):
        try:
            dd_api_key = release.configuration_objects["DD_API_KEY"].read_value(
                config_writer
            )
        except KeyError:
            print("unable to read DD_API_KEY")
        if dd_api_key:
            init_containers.append(render_datadog_container(dd_api_key, datadog_tags))

    env = release.application_environment.environment
    if env and getattr(env, "ephemeral", False):
        node_pool = current_app.config.get("PREVIEW_POOL") or None
    else:
        node_pool = current_app.config.get("STANDARD_POOL") or None

    node_selector = {"cabotage.dev/node-pool": node_pool} if node_pool else None
    tolerations = (
        [
            kubernetes.client.V1Toleration(
                key="cabotage.dev/node-pool",
                value=node_pool,
                effect="NoSchedule",
            ),
        ]
        if node_pool
        else None
    )

    return kubernetes.client.V1PodSpec(
        service_account_name=service_account_name,
        node_selector=node_selector,
        tolerations=tolerations,
        init_containers=init_containers,
        containers=containers,
        volumes=volumes,
        restart_policy=restart_policy,
    )


def render_deployment(
    namespace, release, service_account_name, process_name, deployment_id
):
    label_value = k8s_label_value(release)
    resource_prefix = k8s_resource_prefix(release)
    app_env = release.application_environment
    env_slug = app_env.environment.slug if app_env.environment else ""
    process_counts = app_env.process_counts or {}
    safe_labels = _safe_labels_from_release(release)
    pod_labels = {
        "organization": release.application.project.organization.slug,
        "project": release.application.project.slug,
        "application": release.application.slug,
        "process": process_name,
        "app": label_value,
        "environment": env_slug,
        "release": str(release.version),
        "deployment": str(deployment_id),
        "ca-admission.cabotage.io": "true",
        "resident-pod.cabotage.io": "true",
        **safe_labels,
    }
    deployment_object = kubernetes.client.V1Deployment(
        metadata=kubernetes.client.V1ObjectMeta(
            name=f"{resource_prefix}-{process_name}",
            labels={
                "organization": release.application.project.organization.slug,
                "project": release.application.project.slug,
                "application": release.application.slug,
                "process": process_name,
                "app": label_value,
                "resident-deployment.cabotage.io": "true",
                **safe_labels,
            },
        ),
        spec=kubernetes.client.V1DeploymentSpec(
            replicas=process_counts.get(process_name, 0),
            selector=kubernetes.client.V1LabelSelector(
                match_labels={
                    "app": label_value,
                    "process": process_name,
                },
            ),
            template=kubernetes.client.V1PodTemplateSpec(
                metadata=kubernetes.client.V1ObjectMeta(labels=pod_labels),
                spec=render_podspec(release, process_name, service_account_name),
            ),
        ),
    )
    return deployment_object


def fetch_deployment(
    apps_api_instance, namespace, release, service_account_name, process_name
):
    deployment_object = render_deployment(
        namespace, release, service_account_name, process_name, deployment_id=0
    )
    deployment = None
    try:
        deployment = apps_api_instance.read_namespaced_deployment(
            deployment_object.metadata.name, namespace
        )
    except ApiException as exc:
        if exc.status == 404:
            pass
        else:
            raise DeployError(
                "Unexpected exception fetching Deployment/"
                f"{deployment_object.metadata.name} in {namespace}: {exc}"
            )
    return deployment


def _zero_if_none(value):
    return value if value is not None else 0


def deployment_is_complete(
    apps_api_instance, namespace, release, service_account_name, process_name
):
    deployment = fetch_deployment(
        apps_api_instance, namespace, release, service_account_name, process_name
    )
    return (
        _zero_if_none(deployment.status.updated_replicas)
        == _zero_if_none(deployment.spec.replicas)
        and _zero_if_none(deployment.status.replicas)
        == _zero_if_none(deployment.spec.replicas)
        and _zero_if_none(deployment.status.available_replicas)
        == _zero_if_none(deployment.spec.replicas)
        and deployment.status.observed_generation >= deployment.metadata.generation
    )


def create_deployment(
    apps_api_instance,
    namespace,
    release,
    service_account_name,
    process_name,
    deployment_id,
):
    deployment_object = render_deployment(
        namespace, release, service_account_name, process_name, deployment_id
    )
    deployment = None
    try:
        deployment = apps_api_instance.read_namespaced_deployment(
            deployment_object.metadata.name, namespace
        )
    except ApiException as exc:
        if exc.status == 404:
            pass
        else:
            raise DeployError(
                "Unexpected exception fetching Deployment/"
                f"{deployment_object.metadata.name} in {namespace}: {exc}"
            )
    if deployment is None:
        try:
            return apps_api_instance.create_namespaced_deployment(
                namespace, deployment_object
            )
        except Exception as exc:
            raise DeployError(
                "Unexpected exception creating Deployment/"
                f"{deployment_object.metadata.name} in {namespace}: {exc}"
            )
    else:
        try:
            # Use call_api directly to force application/merge-patch+json.
            # The default patch method uses strategic merge patch, which
            # merges container lists by name and never removes containers
            # absent from the new spec. JSON merge patch (RFC 7386) replaces
            # arrays entirely, ensuring stale containers/initContainers are
            # cleared.
            body = apps_api_instance.api_client.sanitize_for_serialization(
                deployment_object
            )
            return apps_api_instance.api_client.call_api(
                "/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
                "PATCH",
                path_params={
                    "namespace": namespace,
                    "name": deployment_object.metadata.name,
                },
                body=body,
                header_params={
                    "Content-Type": "application/merge-patch+json",
                    "Accept": "application/json",
                },
                response_type="V1Deployment",
                auth_settings=["BearerToken"],
                _return_http_data_only=True,
            )
        except Exception as exc:
            raise DeployError(
                "Unexpected exception patching Deployment/"
                f"{deployment_object.metadata.name} in {namespace}: {exc}"
            )


def scale_deployment(namespace, release, process_name, replicas):
    api_client = kubernetes_ext.kubernetes_client
    apps_api_instance = kubernetes.client.AppsV1Api(api_client)
    deployment_name = f"{k8s_resource_prefix(release)}-{process_name}"
    deployment = None
    try:
        deployment = apps_api_instance.read_namespaced_deployment(
            deployment_name, namespace
        )
    except ApiException as exc:
        if exc.status == 404:
            pass
    if deployment is not None:
        scale = kubernetes.client.V1Scale(
            spec=kubernetes.client.V1ScaleSpec(replicas=replicas)
        )
        apps_api_instance.patch_namespaced_deployment_scale(
            deployment_name, namespace, scale
        )


def resize_deployment(namespace, release, process_name, pod_class_name):
    """Patch a deployment's container resources to match a new pod class."""
    pod_class = pod_classes[pod_class_name]
    api_client = kubernetes_ext.kubernetes_client
    apps_api_instance = kubernetes.client.AppsV1Api(api_client)
    deployment_name = f"{k8s_resource_prefix(release)}-{process_name}"
    try:
        apps_api_instance.read_namespaced_deployment(deployment_name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return
        raise
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": process_name,
                            "resources": {
                                "limits": {
                                    "cpu": pod_class["cpu"]["limits"],
                                    "memory": pod_class["memory"]["limits"],
                                },
                                "requests": {
                                    "cpu": pod_class["cpu"]["requests"],
                                    "memory": pod_class["memory"]["requests"],
                                },
                            },
                        }
                    ]
                }
            }
        }
    }
    apps_api_instance.patch_namespaced_deployment(deployment_name, namespace, patch)


def render_job(namespace, release, service_account_name, process_name, job_id):
    label_value = k8s_label_value(release)
    safe_labels = _safe_labels_from_release(release)
    job_object = kubernetes.client.V1Job(
        metadata=kubernetes.client.V1ObjectMeta(
            name=f"deployment-{job_id}",
            labels={
                "organization": release.application.project.organization.slug,
                "project": release.application.project.slug,
                "application": release.application.slug,
                "process": process_name,
                "app": label_value,
                "release": str(release.version),
                "deployment": job_id,
                "resident-job.cabotage.io": "true",
                **safe_labels,
            },
        ),
        spec=kubernetes.client.V1JobSpec(
            active_deadline_seconds=1800,
            backoff_limit=0,
            parallelism=1,
            template=kubernetes.client.V1PodTemplateSpec(
                metadata=kubernetes.client.V1ObjectMeta(
                    labels={
                        "organization": release.application.project.organization.slug,
                        "project": release.application.project.slug,
                        "application": release.application.slug,
                        "process": process_name,
                        "app": label_value,
                        "release": str(release.version),
                        "deployment": job_id,
                        "ca-admission.cabotage.io": "true",
                        "resident-pod.cabotage.io": "true",
                        **safe_labels,
                    }
                ),
                spec=render_podspec(release, process_name, service_account_name),
            ),
        ),
    )
    return job_object


def _get_job_schedule(process_def):
    """Extract the SCHEDULE env var from a job process definition."""
    for key, value in process_def.get("env", []):
        if key == "SCHEDULE":
            return value
    return None


def _history_limit_for_schedule(schedule, hours=12):
    """Estimate how many times a cron schedule fires in the given window."""
    from croniter import croniter
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)
    it = croniter(schedule, now)
    count = 0
    while it.get_next(datetime) <= end:
        count += 1
    return max(count, 3)


def render_cronjob(
    namespace, release, service_account_name, process_name, deployment_id
):
    label_value = k8s_label_value(release)
    resource_prefix = k8s_resource_prefix(release)
    app_env = release.application_environment
    env_slug = app_env.environment.slug if app_env.environment else ""
    process_def = release.job_processes.get(process_name, {})
    schedule = _get_job_schedule(process_def)
    if schedule is None:
        raise DeployError(
            f"Job process {process_name} is missing required SCHEDULE env var"
        )
    process_counts = app_env.process_counts or {}
    suspended = process_counts.get(process_name, 0) == 0
    safe_labels = _safe_labels_from_release(release)
    common_labels = {
        "organization": release.application.project.organization.slug,
        "project": release.application.project.slug,
        "application": release.application.slug,
        "process": process_name,
        "app": label_value,
        "environment": env_slug,
        "release": str(release.version),
        "deployment": str(deployment_id),
        **safe_labels,
    }
    job_labels = {
        **common_labels,
        "ca-admission.cabotage.io": "true",
        "resident-job.cabotage.io": "true",
    }
    pod_labels = {
        **common_labels,
        "ca-admission.cabotage.io": "true",
        "resident-pod.cabotage.io": "true",
    }
    cronjob_object = kubernetes.client.V1CronJob(
        metadata=kubernetes.client.V1ObjectMeta(
            name=f"{resource_prefix}-{process_name}",
            labels={
                "organization": release.application.project.organization.slug,
                "project": release.application.project.slug,
                "application": release.application.slug,
                "process": process_name,
                "app": label_value,
                "resident-cronjob.cabotage.io": "true",
                **safe_labels,
            },
        ),
        spec=kubernetes.client.V1CronJobSpec(
            schedule=schedule,
            suspend=suspended,
            concurrency_policy="Forbid",
            successful_jobs_history_limit=_history_limit_for_schedule(schedule),
            failed_jobs_history_limit=_history_limit_for_schedule(schedule),
            job_template=kubernetes.client.V1JobTemplateSpec(
                metadata=kubernetes.client.V1ObjectMeta(labels=job_labels),
                spec=kubernetes.client.V1JobSpec(
                    active_deadline_seconds=3600,
                    backoff_limit=0,
                    template=kubernetes.client.V1PodTemplateSpec(
                        metadata=kubernetes.client.V1ObjectMeta(labels=pod_labels),
                        spec=render_podspec(
                            release, process_name, service_account_name
                        ),
                    ),
                ),
            ),
        ),
    )
    return cronjob_object


def create_cronjob(
    batch_api_instance,
    namespace,
    release,
    service_account_name,
    process_name,
    deployment_id,
):
    cronjob_object = render_cronjob(
        namespace, release, service_account_name, process_name, deployment_id
    )
    existing = None
    try:
        existing = batch_api_instance.read_namespaced_cron_job(
            cronjob_object.metadata.name, namespace
        )
    except ApiException as exc:
        if exc.status == 404:
            pass
        else:
            raise DeployError(
                "Unexpected exception fetching CronJob/"
                f"{cronjob_object.metadata.name} in {namespace}: {exc}"
            )
    if existing is None:
        try:
            return batch_api_instance.create_namespaced_cron_job(
                namespace, cronjob_object
            )
        except Exception as exc:
            raise DeployError(
                "Unexpected exception creating CronJob/"
                f"{cronjob_object.metadata.name} in {namespace}: {exc}"
            )
    else:
        try:
            return batch_api_instance.replace_namespaced_cron_job(
                cronjob_object.metadata.name, namespace, cronjob_object
            )
        except Exception as exc:
            raise DeployError(
                "Unexpected exception replacing CronJob/"
                f"{cronjob_object.metadata.name} in {namespace}: {exc}"
            )


def suspend_cronjob(namespace, release, process_name, suspend):
    """Suspend or unsuspend a CronJob."""
    api_client = kubernetes_ext.kubernetes_client
    batch_api_instance = kubernetes.client.BatchV1Api(api_client)
    cronjob_name = f"{k8s_resource_prefix(release)}-{process_name}"
    try:
        batch_api_instance.read_namespaced_cron_job(cronjob_name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return
        raise
    patch = {"spec": {"suspend": suspend}}
    batch_api_instance.patch_namespaced_cron_job(cronjob_name, namespace, patch)


def resize_cronjob(namespace, release, process_name, pod_class_name):
    """Patch a CronJob's container resources to match a new pod class."""
    pod_class = pod_classes[pod_class_name]
    api_client = kubernetes_ext.kubernetes_client
    batch_api_instance = kubernetes.client.BatchV1Api(api_client)
    cronjob_name = f"{k8s_resource_prefix(release)}-{process_name}"
    try:
        batch_api_instance.read_namespaced_cron_job(cronjob_name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return
        raise
    patch = {
        "spec": {
            "jobTemplate": {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": process_name,
                                    "resources": {
                                        "limits": {
                                            "cpu": pod_class["cpu"]["limits"],
                                            "memory": pod_class["memory"]["limits"],
                                        },
                                        "requests": {
                                            "cpu": pod_class["cpu"]["requests"],
                                            "memory": pod_class["memory"]["requests"],
                                        },
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        }
    }
    batch_api_instance.patch_namespaced_cron_job(cronjob_name, namespace, patch)


def fetch_job_logs(core_api_instance, namespace, job_object):
    label_selector = ",".join(
        [f"{k}={v}" for k, v in job_object.spec.template.metadata.labels.items()]
    )
    logs = {}
    try:
        pods = core_api_instance.list_namespaced_pod(
            namespace, label_selector=label_selector
        )
    except ApiException as exc:
        raise DeployError(
            "Unexpected exception listing Pods for Job/"
            f"{job_object.metadata.name} in {namespace}: {exc}"
        )
    for pod in pods.items:
        try:
            pod_logs = core_api_instance.read_namespaced_pod_log(
                pod.metadata.name, namespace, container=pod.metadata.labels["process"]
            )
            logs[pod.metadata.name] = pod_logs
        except ApiException as exc:
            raise DeployError(
                "Unexpected exception reading Pod logs for Job/"
                f"{job_object.metadata.name}/{pod.metadata.name} in {namespace}: {exc}"
            )
    log_string = ""
    for pod_name, log_data in logs.items():
        log_string += f"Job Pod {pod_name}:\n"
        for log_line in log_data.split("\n"):
            log_string += f"  {log_line}\n"
    return log_string


def delete_job(batch_api_instance, namespace, job_object):
    try:
        batch_api_instance.delete_namespaced_job(
            job_object.metadata.name,
            namespace,
            propagation_policy="Foreground",
        )
    except ApiException as exc:
        raise DeployError(
            "Unexpected exception deleting Job/"
            f"{job_object.metadata.name} in {namespace}: {exc}"
        )


def run_job(
    core_api_instance,
    batch_api_instance,
    namespace,
    job_object,
    redis_client=None,
    log_key=None,
    heartbeat_type=None,
    heartbeat_id=None,
    heartbeat_ttl=None,
):
    try:
        batch_api_instance.create_namespaced_job(namespace, job_object)
    except ApiException as exc:
        raise DeployError(
            "Unexpected exception creating Job/"
            f"{job_object.metadata.name} in {namespace}: {exc}"
        )

    if redis_client is not None and log_key is not None:
        return _run_job_streaming(
            core_api_instance,
            batch_api_instance,
            namespace,
            job_object,
            redis_client,
            log_key,
            heartbeat_type=heartbeat_type,
            heartbeat_id=heartbeat_id,
            heartbeat_ttl=heartbeat_ttl,
        )

    try:
        while True:
            job_status = _retry_on_404(
                batch_api_instance.read_namespaced_job_status,
                job_object.metadata.name,
                namespace,
            )
            if job_status.status.failed and job_status.status.failed > 0:
                job_logs = fetch_job_logs(core_api_instance, namespace, job_status)
                delete_job(batch_api_instance, namespace, job_object)
                return False, job_logs
            elif job_status.status.succeeded and job_status.status.succeeded > 0:
                job_logs = fetch_job_logs(core_api_instance, namespace, job_status)
                delete_job(batch_api_instance, namespace, job_object)
                return True, job_logs
            else:
                if heartbeat_type and heartbeat_id and redis_client:
                    refresh_heartbeat(
                        redis_client, heartbeat_type, heartbeat_id, ttl=heartbeat_ttl
                    )
                time.sleep(1)
    finally:
        try:
            delete_job(batch_api_instance, namespace, job_object)
        except (DeployError, ApiException):
            pass


def _run_job_streaming(
    core_api_instance,
    batch_api_instance,
    namespace,
    job_object,
    redis_client,
    log_key,
    heartbeat_type=None,
    heartbeat_id=None,
    heartbeat_ttl=None,
):
    """Run a k8s job, streaming pod logs line-by-line to Redis."""
    job_name = job_object.metadata.name
    log_lines = []

    try:
        # Wait for the job's pod to appear and reach Running state
        label_selector = ",".join(
            f"{k}={v}" for k, v in job_object.spec.template.metadata.labels.items()
        )
        pod = None
        for _ in range(120):  # up to ~2 minutes waiting for pod
            try:
                pods = core_api_instance.list_namespaced_pod(
                    namespace, label_selector=label_selector
                )
                if pods.items:
                    pod = pods.items[0]
                    if pod.status.phase in ("Running", "Succeeded", "Failed"):
                        break
            except ApiException:
                pass
            if heartbeat_type and heartbeat_id:
                refresh_heartbeat(
                    redis_client, heartbeat_type, heartbeat_id, ttl=heartbeat_ttl
                )
            time.sleep(1)

        if pod is None:
            raise DeployError(
                f"Timed out waiting for pod for Job/{job_name} in {namespace}"
            )

        # Stream logs from the pod
        container = job_object.metadata.labels.get("process", None)
        try:
            w = kubernetes.watch.Watch()
            kwargs = dict(
                name=pod.metadata.name,
                namespace=namespace,
                follow=True,
                _preload_content=False,
            )
            if container:
                kwargs["container"] = container
            for line in w.stream(
                core_api_instance.read_namespaced_pod_log,
                **kwargs,
            ):
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                line = line.rstrip("\n")
                publish_log_line(redis_client, log_key, line)
                log_lines.append(line)
                if heartbeat_type and heartbeat_id:
                    refresh_heartbeat(
                        redis_client, heartbeat_type, heartbeat_id, ttl=heartbeat_ttl
                    )
        except ApiException:
            # Pod may have already terminated; logs were collected above
            pass

        # Poll for final job status — k8s may not have updated it yet
        succeeded = False
        for _ in range(30):
            job_status = _retry_on_404(
                batch_api_instance.read_namespaced_job_status,
                job_name,
                namespace,
            )
            if job_status.status.succeeded and job_status.status.succeeded > 0:
                succeeded = True
                break
            if job_status.status.failed and job_status.status.failed > 0:
                break
            if heartbeat_type and heartbeat_id:
                refresh_heartbeat(
                    redis_client, heartbeat_type, heartbeat_id, ttl=heartbeat_ttl
                )
            time.sleep(1)

        # Build log string matching fetch_job_logs format
        log_string = f"Job Pod {pod.metadata.name}:\n"
        for log_line in log_lines:
            log_string += f"  {log_line}\n"

        return succeeded, log_string
    finally:
        try:
            delete_job(batch_api_instance, namespace, job_object)
        except (DeployError, ApiException):
            pass


def deploy_release(deployment):
    job_id = secrets.token_hex(4)
    deployment.job_id = job_id
    db.session.add(deployment)
    db.session.commit()
    deploy_log = []

    deployment_id_str = str(deployment.id)
    _timeout = deployment.application_environment.effective_deployment_timeout
    heartbeat_ttl = _timeout + _HEARTBEAT_TTL

    # Set up Redis streaming for live deploy logs
    try:
        redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
        log_key = stream_key("deploy", job_id)
        refresh_heartbeat(redis_client, "deploy", deployment_id_str, ttl=heartbeat_ttl)
    except Exception:
        redis_client = None
        log_key = None

    def log(msg):
        deploy_log.append(msg)
        if redis_client is not None and log_key is not None:
            try:
                publish_log_line(redis_client, log_key, msg)
                refresh_heartbeat(
                    redis_client, "deploy", deployment_id_str, ttl=heartbeat_ttl
                )
            except Exception:
                logging.getLogger(__name__).warning(
                    "Failed to publish deploy log line to redis", exc_info=True
                )

    # Pick up check run from the build pipeline metadata
    check = CheckRun.from_metadata(
        deployment.deploy_metadata,
        deployment.application_environment,
    )
    deploy_path = f"deployments/{deployment.id}"
    release_id = deployment.release.get("id") if deployment.release else None
    release_obj = deployment.release_object
    image_id = (
        release_obj.image_snapshot.id
        if release_obj and release_obj.image_snapshot
        else None
    )
    deploy_links = {"Deployment": deploy_path}
    if release_id:
        deploy_links["Release"] = f"releases/{release_id}"
    if image_id:
        deploy_links["Image"] = f"images/{image_id}"

    check.progress(
        "Deploying...",
        detail="Image and release built successfully.",
        details_url=cabotage_url(check.application, deploy_path),
        **deploy_links,
    )

    try:
        log("Constructing API Clients")
        api_client = kubernetes_ext.kubernetes_client
        core_api_instance = kubernetes.client.CoreV1Api(api_client)
        apps_api_instance = kubernetes.client.AppsV1Api(api_client)
        batch_api_instance = kubernetes.client.BatchV1Api(api_client)
        custom_objects_api_instance = kubernetes.client.CustomObjectsApi(api_client)
        log("Fetching Namespace")
        namespace = fetch_namespace(core_api_instance, deployment.release_object)
        if (
            current_app.config.get("NETWORK_POLICIES_ENABLED")
            # Legacy: skip network policies for the cabotage namespace — cabotage
            # deploys itself here and needs unrestricted cluster access.
            and namespace.metadata.name != "cabotage"
        ):
            log("Ensuring Network Policies")
            networking_api_instance = kubernetes.client.NetworkingV1Api(api_client)
            ensure_network_policies(networking_api_instance, namespace.metadata.name)
        log("Fetching Cabotage CA Cert ConfigMap")
        fetch_cabotage_ca_configmap(core_api_instance, deployment.release_object)
        log("Fetching ServiceAccount")
        service_account = fetch_service_account(
            core_api_instance, deployment.release_object
        )
        log("Fetching CabotageEnrollment")
        enrollment = fetch_cabotage_enrollment(
            custom_objects_api_instance, deployment.release_object
        )
        reconcile_enrollment_read_keys(
            custom_objects_api_instance, enrollment, deployment.release_object, log=log
        )
        if any(
            [
                process_name.startswith("web")
                for process_name in deployment.release_object.processes
            ]
        ):
            log("Fetching web Service(s)")
            for process_name in deployment.release_object.processes:
                if process_name.startswith("web"):
                    log(f"Fetching {process_name} Service")
                    fetch_service(
                        core_api_instance, deployment.release_object, process_name
                    )
        if any(
            [
                process_name.startswith("tcp")
                for process_name in deployment.release_object.processes
            ]
        ):
            log("Fetching tcp Service(s)")
            for process_name in deployment.release_object.processes:
                if process_name.startswith("tcp"):
                    log(f"Fetching {process_name} Service")
                    fetch_service(
                        core_api_instance, deployment.release_object, process_name
                    )
        app_env = deployment.application_environment
        ingress_domain = current_app.config.get("INGRESS_DOMAIN")
        has_ingresses = bool(app_env.ingresses)
        if ingress_domain or has_ingresses:
            hostname_pairs = _ingress_hostname_pairs(app_env)
            changed = False
            # Auto-hostname reconciliation only applies to nginx ingresses
            if ingress_domain:
                for ing in app_env.ingresses:
                    if ing.ingress_class_name != "nginx":
                        continue
                    auto_hosts = [h for h in ing.hosts if h.is_auto_generated]
                    existing_hostnames = {h.hostname for h in ing.hosts}
                    expected = (
                        f"{readable_k8s_hostname(*hostname_pairs)}"
                        f"-{ing.name}.{ingress_domain}"
                    )
                    has_expected = expected in existing_hostnames
                    if not has_expected:
                        # Keep old auto-generated hosts as manual so DNS keeps
                        # working, then add the new canonical auto-generated host.
                        for host in auto_hosts:
                            log(
                                f"Demoting old auto-generated hostname to manual: {host.hostname}"
                            )
                            host.is_auto_generated = False
                        db.session.add(
                            IngressHost(
                                ingress_id=ing.id,
                                hostname=expected,
                                tls_enabled=True,
                                is_auto_generated=True,
                            )
                        )
                        changed = True
            if changed:
                db.session.commit()
                db.session.refresh(app_env)
            networking_api_instance = kubernetes.client.NetworkingV1Api(api_client)
            if changed or not release_obj.ingresses:
                # Re-snapshot from live DB after hostname reconciliation,
                # or for old releases without serialized ingresses.
                ingress_snapshots = [
                    IngressSnapshot(ing.asdict) for ing in app_env.ingresses
                ]
            else:
                ingress_snapshots = [
                    IngressSnapshot(data) for data in release_obj.ingresses.values()
                ]
            ensure_ingresses(
                networking_api_instance,
                namespace=namespace.metadata.name,
                resource_prefix=k8s_resource_prefix(release_obj),
                labels={
                    "organization": release_obj.application.project.organization.slug,
                    "project": release_obj.application.project.slug,
                    "application": release_obj.application.slug,
                    "environment": (
                        app_env.environment.slug if app_env.environment else ""
                    ),
                    "app": k8s_label_value(release_obj),
                },
                ingresses=ingress_snapshots,
                org_k8s_identifier=release_obj.application.project.organization.k8s_identifier,
                org_default_tags=f"tag:{current_app.config.get('TAILSCALE_TAG_PREFIX', 'cabotage')}",
                process_names=(
                    list(release_obj.processes) if release_obj.processes else []
                ),
                cleanup_orphans=True,
                log=log,
            )
        log("Fetching ImagePullSecrets")
        image_pull_secrets = fetch_image_pull_secrets(
            core_api_instance, deployment.release_object
        )
        log("Patching ServiceAccount with ImagePullSecrets")
        service_account = core_api_instance.patch_namespaced_service_account(
            service_account.metadata.name,
            namespace.metadata.name,
            kubernetes.client.V1ServiceAccount(
                image_pull_secrets=[
                    kubernetes.client.V1LocalObjectReference(
                        name=image_pull_secrets.metadata.name
                    )
                ],
            ),
        )
        for release_command in deployment.release_object.release_commands:
            log(f"Running release command {release_command}")
            job_object = render_job(
                namespace.metadata.name,
                deployment.release_object,
                service_account.metadata.name,
                release_command,
                deployment.job_id,
            )
            job_complete, job_logs = run_job(
                core_api_instance,
                batch_api_instance,
                namespace.metadata.name,
                job_object,
                redis_client=redis_client,
                log_key=log_key,
                heartbeat_type="deploy",
                heartbeat_id=deployment_id_str,
                heartbeat_ttl=heartbeat_ttl,
            )
            deploy_log.append(job_logs)
            if not job_complete:
                raise DeployError(f"Release command {release_command} failed!")
            else:
                log(f"Release command {release_command} complete!")
        for process_name in deployment.release_object.processes:
            _pc = deployment.application_environment.process_counts or {}
            log(
                f"Creating deployment for {process_name} "
                f"with {_pc.get(process_name, 0)} "
                "replicas"
            )
            create_deployment(
                apps_api_instance,
                namespace.metadata.name,
                deployment.release_object,
                service_account.metadata.name,
                process_name,
                deployment_id=deployment.id,
            )
        for process_name in deployment.release_object.job_processes:
            _pc = deployment.application_environment.process_counts or {}
            _suspended = "suspended" if _pc.get(process_name, 0) == 0 else "active"
            log(f"Creating CronJob for {process_name} ({_suspended})")
            create_cronjob(
                batch_api_instance,
                namespace.metadata.name,
                deployment.release_object,
                service_account.metadata.name,
                process_name,
                deployment_id=deployment.id,
            )

        log("Waiting on deployment to rollout...")
        start = time.time()
        timeout = deployment.application_environment.effective_deployment_timeout
        _go = {
            process_name: False for process_name in deployment.release_object.processes
        }
        _last_status = {}
        while time.time() - start < timeout:
            time.sleep(2)
            if redis_client is not None:
                refresh_heartbeat(
                    redis_client, "deploy", deployment_id_str, ttl=heartbeat_ttl
                )
            for process_name in deployment.release_object.processes:
                if _go[process_name]:
                    continue
                dep_obj = fetch_deployment(
                    apps_api_instance,
                    namespace.metadata.name,
                    deployment.release_object,
                    service_account.metadata.name,
                    process_name,
                )
                if dep_obj is None:
                    continue
                desired = _zero_if_none(dep_obj.spec.replicas)
                updated = _zero_if_none(dep_obj.status.updated_replicas)
                available = _zero_if_none(dep_obj.status.available_replicas)
                ready = _zero_if_none(dep_obj.status.ready_replicas)
                complete = (
                    updated == desired
                    and _zero_if_none(dep_obj.status.replicas) == desired
                    and available == desired
                    and dep_obj.status.observed_generation
                    >= dep_obj.metadata.generation
                )
                status_tuple = (ready, updated, available, complete)
                if status_tuple != _last_status.get(process_name):
                    _last_status[process_name] = status_tuple
                    if complete:
                        log(f"  {process_name}: {available}/{desired} available — done")
                        _go[process_name] = True
                    else:
                        log(
                            f"  {process_name}: {ready}/{desired} ready, {updated}/{desired} updated, {available}/{desired} available"
                        )
            if all(_go.values()):
                break
        else:
            log("Unable to launch replicas in time")
            log(str(_go))
            raise DeployError("Unable to launch replicas in time")

        # Clean up k8s Deployments and Services for processes no longer in
        # the Procfile (e.g., a "worker" process that was removed).
        active_process_names = list(deployment.release_object.processes.keys())
        active_job_names = list(deployment.release_object.job_processes.keys())
        cleanup_orphaned_deployments_and_services(
            apps_api_instance,
            core_api_instance,
            deployment.release_object,
            active_process_names,
            log=log,
        )
        cleanup_orphaned_cronjobs(
            batch_api_instance,
            deployment.release_object,
            active_job_names,
            log=log,
        )

        # Prune stale keys from process_counts and process_pod_classes
        app_env = deployment.application_environment
        active_set = set(active_process_names) | set(active_job_names)
        pc = dict(app_env.process_counts or {})
        ppc = dict(app_env.process_pod_classes or {})
        stale_pc = set(pc.keys()) - active_set
        stale_ppc = set(ppc.keys()) - active_set
        if stale_pc or stale_ppc:
            for k in stale_pc:
                del pc[k]
            for k in stale_ppc:
                del ppc[k]
            app_env.process_counts = pc
            app_env.process_pod_classes = ppc
            flag_modified(app_env, "process_counts")
            flag_modified(app_env, "process_pod_classes")
            db.session.commit()
            if log is not None:
                log(f"Pruned stale process keys: {stale_pc | stale_ppc}")

        for postdeploy_command in deployment.release_object.postdeploy_commands:
            log(f"Running postdeploy command {postdeploy_command}")
            job_object = render_job(
                namespace.metadata.name,
                deployment.release_object,
                service_account.metadata.name,
                postdeploy_command,
                deployment.job_id,
            )
            job_complete, job_logs = run_job(
                core_api_instance,
                batch_api_instance,
                namespace.metadata.name,
                job_object,
                redis_client=redis_client,
                log_key=log_key,
                heartbeat_type="deploy",
                heartbeat_id=deployment_id_str,
                heartbeat_ttl=heartbeat_ttl,
            )
            deploy_log.append(job_logs)
            if not job_complete:
                raise DeployError(f"Release command {postdeploy_command} failed!")
            else:
                log(f"Release command {postdeploy_command} complete!")
        deployment.complete = True
        log(f"Deployment {deployment.id} complete")
    except DeployError as exc:
        deployment.error = True
        deployment.error_detail = str(exc)
        if (
            deployment.deploy_metadata
            and "installation_id" in deployment.deploy_metadata
            and "statuses_url" in deployment.deploy_metadata
            and not deployment.deploy_metadata.get("branch_deploy")
        ):
            access_token = github_app.fetch_installation_access_token(
                deployment.deploy_metadata["installation_id"]
            )
            post_deployment_status_update(
                access_token,
                deployment.deploy_metadata["statuses_url"],
                "failure",
                f"Deployment failed: {exc}",
            )
        if redis_client is not None and log_key is not None:
            try:
                publish_end(redis_client, log_key, error=True)
            except Exception:
                logging.getLogger(__name__).warning(
                    "Failed to publish deploy log stream end", exc_info=True
                )
        deployment.deploy_log = "\n".join(deploy_log)
        db.session.commit()
        _dispatch_deploy_failure(deployment, str(exc))
        check.fail(
            "Deployment failed",
            detail=str(exc),
            details_url=cabotage_url(check.application, deploy_path),
            Deployment=deploy_path,
        )
        return False
    except Exception as exc:
        deployment.error = True
        deployment.error_detail = "Deploy failed due to an internal error"
        if (
            deployment.deploy_metadata
            and "installation_id" in deployment.deploy_metadata
            and "statuses_url" in deployment.deploy_metadata
            and not deployment.deploy_metadata.get("branch_deploy")
        ):
            access_token = github_app.fetch_installation_access_token(
                deployment.deploy_metadata["installation_id"]
            )
            post_deployment_status_update(
                access_token,
                deployment.deploy_metadata["statuses_url"],
                "error",
                f"Deployment failed: {exc}",
            )
        if redis_client is not None and log_key is not None:
            try:
                publish_end(redis_client, log_key, error=True)
            except Exception:
                logging.getLogger(__name__).warning(
                    "Failed to publish deploy log stream end", exc_info=True
                )
        deployment.deploy_log = "\n".join(deploy_log)
        db.session.commit()
        _dispatch_deploy_failure(deployment, "Deploy failed due to an internal error")
        check.fail(
            "Deployment failed",
            detail=str(exc),
            details_url=cabotage_url(check.application, deploy_path),
            Deployment=deploy_path,
        )
        return False
    if redis_client is not None and log_key is not None:
        try:
            publish_end(redis_client, log_key, error=False)
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to publish deploy log stream end", exc_info=True
            )
    deployment.deploy_log = "\n".join(deploy_log)
    db.session.commit()

    # Resolve preview URL — only furnish it if TLS cert is ready
    app_env = deployment.application_environment
    env_url = _preview_url_for_app_env(app_env)
    if env_url:
        tls_ready = False
        resource_prefix = k8s_resource_prefix(deployment.release_object)
        for ing in app_env.ingresses:
            if ing.enabled and any(
                h.is_auto_generated and h.tls_enabled for h in ing.hosts
            ):
                cert_name = f"{resource_prefix}-{ing.name}-tls"
                log(f"Waiting for TLS certificate ({cert_name})")
                tls_ready = _wait_for_tls_certificate(
                    api_client,
                    namespace.metadata.name,
                    cert_name,
                    log=log,
                )
                break
        if not tls_ready:
            env_url = None

    if (
        deployment.deploy_metadata
        and "installation_id" in deployment.deploy_metadata
        and "statuses_url" in deployment.deploy_metadata
        and not deployment.deploy_metadata.get("branch_deploy")
    ):
        access_token = github_app.fetch_installation_access_token(
            deployment.deploy_metadata["installation_id"]
        )
        post_deployment_status_update(
            access_token,
            deployment.deploy_metadata["statuses_url"],
            "success",
            "Deployment complete!",
            environment_url=env_url,
        )
    detail = f"Application URL: {env_url}" if env_url else ""
    check.succeed(
        detail=detail,
        details_url=cabotage_url(check.application, deploy_path),
        **deploy_links,
    )
    if deployment.deploy_metadata and deployment.deploy_metadata.get("auto_deploy"):
        try:
            image_id = deployment.deploy_metadata.get(
                "source_image_id", str(deployment.id)
            )
            dispatch_autodeploy_notification(
                "complete",
                image_id,
                deployment.application,
                deployment.application_environment,
                image_url=cabotage_url(check.application, f"images/{image_id}"),
                deploy_url=cabotage_url(check.application, deploy_path),
                image_metadata=deployment.deploy_metadata,
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to dispatch autodeploy completion", exc_info=True
            )
    else:
        try:
            dispatch_pipeline_notification.delay(
                "pipeline.deploy",
                "Deployment",
                str(deployment.id),
                str(deployment.application.project.organization_id),
                str(deployment.application.id),
                str(deployment.application_environment_id)
                if deployment.application_environment_id
                else None,
                complete=True,
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to dispatch deploy completion notification", exc_info=True
            )


def fake_deploy_release(deployment):
    deploy_log = []
    namespace = render_namespace(deployment.release_object)
    deploy_log.append(f"Creating Namespace/{namespace.metadata.name}")
    deploy_log.append(yaml.dump(remove_none(namespace.to_dict())))
    service_account = render_service_account(deployment.release_object)
    deploy_log.append(
        f"Creating ServiceAccount/{service_account.metadata.name} "
        f"in Namespace/{namespace.metadata.name}"
    )
    deploy_log.append(yaml.dump(remove_none(service_account.to_dict())))
    cabotage_enrollment = render_cabotage_enrollment(deployment.release_object)
    deploy_log.append(
        f"Creating CabotageEnrollment/{cabotage_enrollment['metadata']['name']} "
        f"in Namespace/{namespace.metadata.name}"
    )
    deploy_log.append(yaml.dump(remove_none(cabotage_enrollment)))
    if any(
        [
            process_name.startswith("web")
            for process_name in deployment.release_object.processes
        ]
    ):
        deploy_log.append("Fetching web Service(s)")
        for process_name in deployment.release_object.processes:
            if process_name.startswith("web"):
                deploy_log.append(f"Fetching {process_name} Service")
                service = render_service(deployment.release_object, process_name)
                deploy_log.append(yaml.dump(remove_none(service.to_dict())))
    if any(
        [
            process_name.startswith("tcp")
            for process_name in deployment.release_object.processes
        ]
    ):
        deploy_log.append("Fetching tcp Service(s)")
        for process_name in deployment.release_object.processes:
            if process_name.startswith("tcp"):
                deploy_log.append(f"Fetching {process_name} Service")
                service = render_service(deployment.release_object, process_name)
                deploy_log.append(yaml.dump(remove_none(service.to_dict())))
    app_env = deployment.application_environment
    ingress_domain = current_app.config.get("INGRESS_DOMAIN")
    has_ingresses = bool(app_env.ingresses)
    if ingress_domain or has_ingresses:
        hostname_pairs = _ingress_hostname_pairs(app_env)
        changed = False
        if ingress_domain:
            for ing in app_env.ingresses:
                if ing.ingress_class_name != "nginx":
                    continue
                auto_hosts = [h for h in ing.hosts if h.is_auto_generated]
                existing_hostnames = {h.hostname for h in ing.hosts}
                expected = (
                    f"{readable_k8s_hostname(*hostname_pairs)}"
                    f"-{ing.name}.{ingress_domain}"
                )
                has_expected = expected in existing_hostnames
                if not has_expected:
                    for host in auto_hosts:
                        deploy_log.append(
                            f"Demoting old auto-generated hostname to manual: {host.hostname}"
                        )
                        host.is_auto_generated = False
                    db.session.add(
                        IngressHost(
                            ingress_id=ing.id,
                            hostname=expected,
                            tls_enabled=True,
                            is_auto_generated=True,
                        )
                    )
                    changed = True
        if changed:
            db.session.commit()
            db.session.refresh(app_env)
        release_obj = deployment.release_object
        if changed or not release_obj.ingresses:
            ingress_snapshots = [
                IngressSnapshot(ing.asdict) for ing in app_env.ingresses
            ]
        else:
            ingress_snapshots = [
                IngressSnapshot(data) for data in release_obj.ingresses.values()
            ]
        for ingress in ingress_snapshots:
            ingress_obj = render_ingress(release_obj, ingress)
            if ingress_obj:
                deploy_log.append(f"Fetching Ingress/{ingress.name}")
                deploy_log.append(yaml.dump(remove_none(ingress_obj.to_dict())))
        active_names = [i.name for i in ingress_snapshots]
        deploy_log.append(
            f"Cleanup: would delete orphaned ingresses not in {active_names}"
        )
    image_pull_secrets = render_image_pull_secrets(deployment.release_object)
    deploy_log.append(
        f"Creating ImagePullSecrets/{image_pull_secrets.metadata.name} "
        f"in Namespace/{namespace.metadata.name}"
    )
    deploy_log.append(yaml.dump(remove_none(image_pull_secrets.to_dict())))
    deploy_log.append(
        f"Patching ServiceAccount/{service_account.metadata.name} "
        f"with ImagePullSecrets/{image_pull_secrets.metadata.name} "
        f"in Namespace/{namespace.metadata.name}"
    )
    for release_command in deployment.release_object.release_commands:
        job_object = render_job(
            namespace.metadata.name,
            deployment.release_object,
            service_account.metadata.name,
            release_command,
            deployment.job_id,
        )
        deploy_log.append(
            f"Running Job/{job_object.metadata.name} "
            f"in Namespace/{namespace.metadata.name}"
        )
        deploy_log.append(yaml.dump(remove_none(job_object.to_dict())))
    for process in deployment.release_object.processes:
        deployment_object = render_deployment(
            namespace.metadata.name,
            deployment.release_object,
            service_account.metadata.name,
            process,
            deployment_id=deployment.id,
        )
        deploy_log.append(
            f"Creating Deployment/{deployment_object.metadata.name} "
            f"in Namespace/{namespace.metadata.name}"
        )
        deploy_log.append(yaml.dump(remove_none(deployment_object.to_dict())))
    for process in deployment.release_object.job_processes:
        cronjob_object = render_cronjob(
            namespace.metadata.name,
            deployment.release_object,
            service_account.metadata.name,
            process,
            deployment_id=deployment.id,
        )
        deploy_log.append(
            f"Creating CronJob/{cronjob_object.metadata.name} "
            f"in Namespace/{namespace.metadata.name}"
        )
        deploy_log.append(yaml.dump(remove_none(cronjob_object.to_dict())))
    deployment.deploy_log = "\n".join(deploy_log)
    db.session.commit()
    if (
        deployment.deploy_metadata
        and "installation_id" in deployment.deploy_metadata
        and "statuses_url" in deployment.deploy_metadata
        and not deployment.deploy_metadata.get("branch_deploy")
    ):
        access_token = github_app.fetch_installation_access_token(
            deployment.deploy_metadata["installation_id"]
        )
        post_deployment_status_update(
            access_token,
            deployment.deploy_metadata["statuses_url"],
            "success",
            "Deployment complete!",
        )


def remove_none(obj):
    if isinstance(obj, (list, tuple, set)):
        return type(obj)(remove_none(x) for x in obj if x is not None)
    elif isinstance(obj, dict):
        return type(obj)(
            (remove_none(k), remove_none(v))
            for k, v in obj.items()
            if k is not None and v is not None
        )
    else:
        return obj


@shared_task(acks_late=True)
def run_deploy(deployment_id=None):
    deployment = Deployment.query.filter_by(id=deployment_id).first()
    if deployment is None:
        raise KeyError(f"Deployment with ID {deployment_id} not found!")
    error_detail = ""
    try:
        deploy_release(deployment)
    except DeployError as exc:
        error_detail = str(exc)
        print(error_detail)
        print(exc)
    except Exception:
        raise
