import base64
import hashlib
import logging
import secrets
import struct

import kubernetes
from celery import shared_task
from flask import current_app, has_app_context
from kubernetes.client.rest import ApiException
from sqlalchemy import text

from cabotage.server import (
    config_writer,
    db,
    kubernetes as kubernetes_ext,
)
from cabotage.server.config import validate_tenant_postgres_backup_config
from cabotage.server.models.resources import (
    postgres_size_classes,
    redis_size_classes,
)
from cabotage.server.models.utils import safe_k8s_name
from cabotage.celery.tasks.deploy import ensure_namespace, ensure_network_policies

log = logging.getLogger(__name__)

_RECONCILE_LOCK_KEY = struct.unpack(
    ">q", hashlib.sha256(b"cabotage:reconcile_backing_services").digest()[:8]
)[0]

# ---------------------------------------------------------------------------
# CNPG (CloudNativePG) constants
# ---------------------------------------------------------------------------
CNPG_GROUP = "postgresql.cnpg.io"
CNPG_VERSION = "v1"
CNPG_PLURAL = "clusters"
CNPG_SCHEDULED_BACKUP_PLURAL = "scheduledbackups"

# ---------------------------------------------------------------------------
# Barman Cloud Plugin constants
# ---------------------------------------------------------------------------
BARMAN_GROUP = "barmancloud.cnpg.io"
BARMAN_VERSION = "v1"
BARMAN_OBJECT_STORE_PLURAL = "objectstores"

# ---------------------------------------------------------------------------
# OpsTree Redis Operator constants
# ---------------------------------------------------------------------------
REDIS_GROUP = "redis.redis.opstreelabs.in"
REDIS_VERSION = "v1beta2"
REDIS_STANDALONE_PLURAL = "redis"
REDIS_CLUSTER_PLURAL = "redisclusters"

# ---------------------------------------------------------------------------
# cert-manager constants
# ---------------------------------------------------------------------------
CERTMANAGER_GROUP = "cert-manager.io"
CERTMANAGER_VERSION = "v1"
CERTMANAGER_CERT_PLURAL = "certificates"

# ClusterIssuer used to sign TLS certs for backing services
TLS_CLUSTER_ISSUER = "operators-ca-issuer"
# Secret containing the CA cert for TLS verification
TLS_CA_SECRET = "operators-ca-crt"  # nosec B105
NODE_POOL_LABEL = "cabotage.dev/node-pool"
DO_NOT_DISRUPT_ANNOTATION = "karpenter.sh/do-not-disrupt"
HOSTNAME_TOPOLOGY_KEY = "kubernetes.io/hostname"
ZONE_TOPOLOGY_KEY = "topology.kubernetes.io/zone"

REDIS_IMAGES: dict[str, str] = {
    "8": "quay.io/opstree/redis:v8.6.2",
}

POSTGRES_IMAGES: dict[str, str] = {
    "18": "ghcr.io/cloudnative-pg/postgresql:18.3-202604060836-minimal-trixie@sha256:3b36ae680b6ed02e5a44b4a2eaf1d1a002e1a7d581bbd5171fd5f9d94082a361",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resource_namespace(resource):
    return resource.environment.k8s_namespace


def _resource_k8s_name(resource):
    project = resource.environment.project
    return safe_k8s_name(project.k8s_identifier, resource.k8s_identifier)


def _set_if_changed(obj, attr, value):
    if getattr(obj, attr) != value:
        setattr(obj, attr, value)


def _resource_labels(resource):
    """Labels applied to the operator CRD object itself."""
    org = resource.environment.project.organization
    project = resource.environment.project
    env = resource.environment
    return {
        # Slug-based (human-readable)
        "organization": org.slug,
        "project": project.slug,
        "environment": env.slug,
        # Safe k8s-identifier labels (collision-safe)
        "cabotage.io/organization": org.k8s_identifier,
        "cabotage.io/project": project.k8s_identifier,
        "cabotage.io/environment": env.k8s_identifier,
        # Resource tracking
        "cabotage.io/resource-id": str(resource.id),
        "cabotage.io/resource-type": resource.type,
        # Resident label for cleanup queries
        f"resident-{resource.type}.cabotage.io": "true",
    }


def _resource_pod_labels(resource):
    """Labels applied to pods managed by the operator (via inheritedMetadata)."""
    labels = _resource_labels(resource)
    labels["backing-service"] = "true"
    labels["backing-service-type"] = resource.type
    labels["backing-service-slug"] = resource.slug
    labels["resident-pod.cabotage.io"] = "true"
    labels["ca-admission.cabotage.io"] = "true"
    return labels


def _tls_secret_name(resource):
    return f"{_resource_k8s_name(resource)}-tls"


def _password_secret_name(resource):
    return f"{_resource_k8s_name(resource)}-password"


def _backing_services_pool():
    if not has_app_context():
        return None
    return current_app.config.get("BACKING_SERVICES_POOL") or None


def _backing_service_type_enabled(resource_type):
    config_key = {
        "postgres": "BACKING_SERVICE_POSTGRES_ENABLED",
        "redis": "BACKING_SERVICE_REDIS_ENABLED",
    }.get(resource_type)
    if config_key is None:
        raise KeyError(f"Unknown backing service type: {resource_type}")
    if not has_app_context():
        return True
    return bool(current_app.config.get(config_key, True))


def _tenant_postgres_backups_enabled(resource=None):
    if not has_app_context():
        return False
    if not current_app.config.get("TENANT_POSTGRES_BACKUPS_ENABLED"):
        return False
    if resource is not None and getattr(resource, "backup_strategy", None) == "none":
        return False
    return True


def _postgres_backup_requires_continuous_archiving(resource):
    return getattr(resource, "backup_strategy", None) == "streaming"


def _tenant_postgres_backup_settings():
    if not _tenant_postgres_backups_enabled():
        return None

    validate_tenant_postgres_backup_config(current_app.config)

    return {
        "provider": current_app.config["TENANT_POSTGRES_BACKUP_PROVIDER"]
        .strip()
        .lower(),
        "bucket": current_app.config["TENANT_POSTGRES_BACKUP_BUCKET"],
        "irsa_role_arn": current_app.config.get("TENANT_POSTGRES_BACKUP_IRSA_ROLE_ARN"),
        "path_prefix": current_app.config["TENANT_POSTGRES_BACKUP_PATH_PREFIX"],
        "plugin_name": current_app.config["TENANT_POSTGRES_BACKUP_PLUGIN_NAME"],
        "retention_policy": current_app.config[
            "TENANT_POSTGRES_BACKUP_RETENTION_POLICY"
        ],
        "schedule": current_app.config["TENANT_POSTGRES_BACKUP_SCHEDULE"],
        "service_account_name": current_app.config[
            "TENANT_POSTGRES_BACKUP_SERVICE_ACCOUNT_NAME"
        ],
        "rustfs_endpoint": current_app.config.get(
            "TENANT_POSTGRES_BACKUP_RUSTFS_ENDPOINT"
        ),
        "rustfs_ca_secret_name": current_app.config.get(
            "TENANT_POSTGRES_BACKUP_RUSTFS_CA_SECRET_NAME"
        ),
        "rustfs_secret_name": current_app.config.get(
            "TENANT_POSTGRES_BACKUP_RUSTFS_SECRET_NAME"
        ),
        "rustfs_source_secret_name": current_app.config.get(
            "TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAME"
        ),
        "rustfs_source_secret_namespace": current_app.config.get(
            "TENANT_POSTGRES_BACKUP_RUSTFS_SOURCE_SECRET_NAMESPACE"
        ),
    }


def _node_selector_for_pool(node_pool):
    if not node_pool:
        return None
    return {NODE_POOL_LABEL: node_pool}


def _tolerations_for_pool(node_pool):
    if not node_pool:
        return None
    return [
        {
            "key": NODE_POOL_LABEL,
            "operator": "Equal",
            "value": node_pool,
            "effect": "NoSchedule",
        }
    ]


def _backing_service_pod_annotations():
    if not _backing_services_pool():
        return {}
    return {DO_NOT_DISRUPT_ANNOTATION: "true"}


def _preferred_pod_anti_affinity_term(match_labels, topology_key, weight=100):
    return {
        "weight": weight,
        "podAffinityTerm": {
            "labelSelector": {"matchLabels": match_labels},
            "topologyKey": topology_key,
        },
    }


def _required_same_resource_role_anti_affinity(resource, role):
    return [
        {
            "labelSelector": {
                "matchLabels": {
                    "cabotage.io/resource-id": str(resource.id),
                    "role": role,
                }
            },
            "topologyKey": HOSTNAME_TOPOLOGY_KEY,
        }
    ]


def _cnpg_affinity(resource):
    node_pool = _backing_services_pool()
    if not node_pool:
        return None

    affinity = {
        "nodeSelector": _node_selector_for_pool(node_pool),
        "tolerations": _tolerations_for_pool(node_pool),
    }
    if resource.ha_enabled:
        preferred_terms = [
            _preferred_pod_anti_affinity_term(
                {"resident-pod.cabotage.io": "true"},
                HOSTNAME_TOPOLOGY_KEY,
            ),
            _preferred_pod_anti_affinity_term(
                {"cabotage.io/resource-id": str(resource.id)},
                ZONE_TOPOLOGY_KEY,
                weight=90,
            ),
        ]
        affinity.update(
            {
                "enablePodAntiAffinity": True,
                "podAntiAffinityType": "required",
                "topologyKey": HOSTNAME_TOPOLOGY_KEY,
                "additionalPodAntiAffinity": {
                    "preferredDuringSchedulingIgnoredDuringExecution": preferred_terms,
                },
            }
        )
    return affinity


def _backup_objectstore_name(resource):
    return f"{_resource_k8s_name(resource)}-backups"


def _backup_destination_path(namespace, cluster_name, settings):
    path_prefix = settings["path_prefix"].strip("/")
    path_parts = [part for part in (path_prefix, namespace, cluster_name) if part]
    return f"s3://{settings['bucket']}/{'/'.join(path_parts)}/"


def _render_postgres_object_store(resource, settings):
    namespace = _resource_namespace(resource)
    cluster_name = _resource_k8s_name(resource)
    configuration = {
        "destinationPath": _backup_destination_path(namespace, cluster_name, settings),
        "data": {"compression": "gzip"},
        "wal": {"compression": "gzip"},
    }

    if settings["provider"] == "s3":
        configuration["s3Credentials"] = {"inheritFromIAMRole": True}
    else:
        configuration.update(
            {
                "endpointURL": settings["rustfs_endpoint"],
                "endpointCA": {
                    "name": settings["rustfs_ca_secret_name"],
                    "key": "ca.crt",
                },
                "s3Credentials": {
                    "accessKeyId": {
                        "name": settings["rustfs_secret_name"],
                        "key": "access-key-id",
                    },
                    "secretAccessKey": {
                        "name": settings["rustfs_secret_name"],
                        "key": "secret-key",
                    },
                    "region": {
                        "name": settings["rustfs_secret_name"],
                        "key": "region",
                    },
                },
            }
        )

    return {
        "apiVersion": f"{BARMAN_GROUP}/{BARMAN_VERSION}",
        "kind": "ObjectStore",
        "metadata": {
            "name": _backup_objectstore_name(resource),
            "labels": _resource_labels(resource),
        },
        "spec": {
            "retentionPolicy": settings["retention_policy"],
            "configuration": configuration,
        },
    }


def _render_scheduled_backup(resource, settings, immediate=False):
    body = {
        "apiVersion": f"{CNPG_GROUP}/{CNPG_VERSION}",
        "kind": "ScheduledBackup",
        "metadata": {
            "name": _resource_k8s_name(resource),
            "labels": _resource_labels(resource),
        },
        "spec": {
            "schedule": settings["schedule"],
            "backupOwnerReference": "self",
            "cluster": {
                "name": _resource_k8s_name(resource),
            },
            "method": "plugin",
            "pluginConfiguration": {
                "name": settings["plugin_name"],
            },
            "target": "prefer-standby",
        },
    }
    if immediate:
        body["spec"]["immediate"] = True
    return body


def _ensure_scheduled_backup(custom_api, namespace, resource, settings):
    name = _resource_k8s_name(resource)
    try:
        custom_api.get_namespaced_custom_object(
            CNPG_GROUP,
            CNPG_VERSION,
            namespace,
            CNPG_SCHEDULED_BACKUP_PLURAL,
            name,
        )
        custom_api.patch_namespaced_custom_object(
            CNPG_GROUP,
            CNPG_VERSION,
            namespace,
            CNPG_SCHEDULED_BACKUP_PLURAL,
            name,
            _render_scheduled_backup(resource, settings, immediate=False),
        )
        log.info(
            "Patched %s/%s %s/%s",
            CNPG_GROUP,
            CNPG_SCHEDULED_BACKUP_PLURAL,
            namespace,
            name,
        )
    except ApiException as exc:
        if exc.status != 404:
            raise
        custom_api.create_namespaced_custom_object(
            CNPG_GROUP,
            CNPG_VERSION,
            namespace,
            CNPG_SCHEDULED_BACKUP_PLURAL,
            _render_scheduled_backup(resource, settings, immediate=True),
        )
        log.info(
            "Created %s/%s %s/%s",
            CNPG_GROUP,
            CNPG_SCHEDULED_BACKUP_PLURAL,
            namespace,
            name,
        )


def _redis_role_affinity(resource, role, replicas):
    node_pool = _backing_services_pool()
    if not node_pool:
        return None

    preferred_terms = [
        _preferred_pod_anti_affinity_term(
            {"resident-pod.cabotage.io": "true"},
            HOSTNAME_TOPOLOGY_KEY,
        )
    ]
    if replicas > 1:
        preferred_terms.append(
            _preferred_pod_anti_affinity_term(
                {
                    "cabotage.io/resource-id": str(resource.id),
                    "role": role,
                },
                ZONE_TOPOLOGY_KEY,
                weight=90,
            )
        )

    pod_anti_affinity = {
        "preferredDuringSchedulingIgnoredDuringExecution": preferred_terms,
    }
    if replicas > 1:
        pod_anti_affinity["requiredDuringSchedulingIgnoredDuringExecution"] = (
            _required_same_resource_role_anti_affinity(resource, role)
        )

    return {"podAntiAffinity": pod_anti_affinity}


def _redis_statefulset_names(resource, name):
    if resource.ha_enabled:
        return [f"{name}-leader", f"{name}-follower"]
    return [name]


def _sync_statefulset_pod_annotations(apps_api, namespace, statefulset_name):
    managed_keys = {DO_NOT_DISRUPT_ANNOTATION}
    desired_annotations = _backing_service_pod_annotations()

    try:
        statefulset = apps_api.read_namespaced_stateful_set(statefulset_name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            log.info(
                "StatefulSet %s/%s not yet available for pod annotation sync",
                namespace,
                statefulset_name,
            )
            return
        raise

    metadata = statefulset.spec.template.metadata
    current_annotations = dict((metadata.annotations or {}))
    annotations = {
        key: value
        for key, value in current_annotations.items()
        if key not in managed_keys
    }
    annotations.update(desired_annotations)

    if annotations == current_annotations:
        return

    apps_api.patch_namespaced_stateful_set(
        statefulset_name,
        namespace,
        {"spec": {"template": {"metadata": {"annotations": annotations}}}},
    )
    log.info(
        "Patched StatefulSet %s/%s pod annotations for backing-service placement",
        namespace,
        statefulset_name,
    )


# ---------------------------------------------------------------------------
# TLS Certificate (cert-manager) rendering
# ---------------------------------------------------------------------------


def _render_postgres_certificate(resource):
    """Render a cert-manager Certificate for a CNPG Cluster."""
    name = _resource_k8s_name(resource)
    namespace = _resource_namespace(resource)
    secret_name = _tls_secret_name(resource)

    # DNS names for the CNPG service endpoints
    dns_names = []
    for suffix in (f"{name}-rw", f"{name}-r", f"{name}-ro"):
        dns_names.append(suffix)
        dns_names.append(f"{suffix}.{namespace}")
        dns_names.append(f"{suffix}.{namespace}.svc")
        dns_names.append(f"{suffix}.{namespace}.svc.cluster.local")

    return {
        "apiVersion": f"{CERTMANAGER_GROUP}/{CERTMANAGER_VERSION}",
        "kind": "Certificate",
        "metadata": {
            "name": name,
            "labels": _resource_labels(resource),
        },
        "spec": {
            "secretName": secret_name,
            "duration": "2160h",  # 90 days
            "renewBefore": "360h",  # 15 days
            "subject": {"organizations": ["cabotage"]},
            "commonName": f"{name}-primary",
            "isCA": False,
            "privateKey": {"algorithm": "ECDSA", "size": 256},
            "usages": ["digital signature", "key encipherment"],
            "dnsNames": dns_names,
            "issuerRef": {
                "name": TLS_CLUSTER_ISSUER,
                "kind": "ClusterIssuer",
                "group": CERTMANAGER_GROUP,
            },
        },
    }


def _service_dns_names(service_name, namespace):
    return [
        service_name,
        f"{service_name}.{namespace}",
        f"{service_name}.{namespace}.svc",
        f"{service_name}.{namespace}.svc.cluster.local",
    ]


def _add_pod_dns_names(dns_names, pod_name, namespace, service_names=()):
    dns_names.update(
        [
            pod_name,
            f"{pod_name}.{namespace}",
            f"{pod_name}.{namespace}.svc",
            f"{pod_name}.{namespace}.svc.cluster.local",
        ]
    )
    for service_name in service_names:
        dns_names.update(
            [
                f"{pod_name}.{service_name}",
                f"{pod_name}.{service_name}.{namespace}",
                f"{pod_name}.{service_name}.{namespace}.svc",
                f"{pod_name}.{service_name}.{namespace}.svc.cluster.local",
            ]
        )


def _render_redis_certificate(resource):
    """Render a cert-manager Certificate for a Redis instance."""
    name = _resource_k8s_name(resource)
    namespace = _resource_namespace(resource)
    secret_name = _tls_secret_name(resource)

    if resource.ha_enabled:
        service_names = [
            f"{name}-master",
            f"{name}-leader",
            f"{name}-leader-additional",
            f"{name}-leader-headless",
            f"{name}-follower",
            f"{name}-follower-additional",
            f"{name}-follower-headless",
        ]
        dns_names = set()
        for service_name in service_names:
            dns_names.update(_service_dns_names(service_name, namespace))

        pod_service_names = [
            f"{name}-master",
            f"{name}-leader",
            f"{name}-leader-headless",
            f"{name}-follower",
            f"{name}-follower-headless",
        ]
        for i in range(resource.leader_replicas):
            _add_pod_dns_names(
                dns_names,
                f"{name}-leader-{i}",
                namespace,
                pod_service_names,
            )
        for i in range(resource.follower_replicas):
            _add_pod_dns_names(
                dns_names,
                f"{name}-follower-{i}",
                namespace,
                pod_service_names,
            )
    else:
        dns_names = set()
        service_names = [
            name,
            f"{name}-additional",
            f"{name}-headless",
        ]
        for service_name in service_names:
            dns_names.update(_service_dns_names(service_name, namespace))
        _add_pod_dns_names(
            dns_names,
            f"{name}-0",
            namespace,
            [name, f"{name}-headless"],
        )

    return {
        "apiVersion": f"{CERTMANAGER_GROUP}/{CERTMANAGER_VERSION}",
        "kind": "Certificate",
        "metadata": {
            "name": name,
            "labels": _resource_labels(resource),
        },
        "spec": {
            "secretName": secret_name,
            "duration": "2160h",
            "renewBefore": "360h",
            "subject": {"organizations": ["cabotage"]},
            "commonName": name,
            "isCA": False,
            "privateKey": {"algorithm": "ECDSA", "size": 256},
            "usages": ["digital signature", "key encipherment"],
            "dnsNames": sorted(dns_names),
            "issuerRef": {
                "name": TLS_CLUSTER_ISSUER,
                "kind": "ClusterIssuer",
                "group": CERTMANAGER_GROUP,
            },
        },
    }


# ---------------------------------------------------------------------------
# K8s prerequisite helpers
# ---------------------------------------------------------------------------


def _ensure_certificate(custom_api, namespace, cert_body):
    """Create or patch a cert-manager Certificate custom resource."""
    name = cert_body["metadata"]["name"]
    _ensure_custom_object(
        custom_api,
        CERTMANAGER_GROUP,
        CERTMANAGER_VERSION,
        namespace,
        CERTMANAGER_CERT_PLURAL,
        name,
        cert_body,
    )


def _ensure_password_secret(core_api, namespace, secret_name, labels):
    """Create a Kubernetes Secret with a random password if it doesn't exist."""
    try:
        core_api.read_namespaced_secret(secret_name, namespace)
        log.info("Password Secret %s/%s already exists", namespace, secret_name)
    except ApiException as exc:
        if exc.status == 404:
            password = secrets.token_urlsafe(48)
            core_api.create_namespaced_secret(
                namespace,
                kubernetes.client.V1Secret(
                    metadata=kubernetes.client.V1ObjectMeta(
                        name=secret_name,
                        namespace=namespace,
                        labels=labels,
                    ),
                    string_data={"password": password},
                ),
            )
            log.info("Created password Secret %s/%s", namespace, secret_name)
        else:
            raise


def _ensure_custom_object(custom_api, group, version, namespace, plural, name, body):
    """Create or patch a namespaced custom object."""
    try:
        custom_api.get_namespaced_custom_object(
            group,
            version,
            namespace,
            plural,
            name,
        )
        custom_api.patch_namespaced_custom_object(
            group,
            version,
            namespace,
            plural,
            name,
            body,
        )
        log.info("Patched %s/%s %s/%s", group, plural, namespace, name)
    except ApiException as exc:
        if exc.status != 404:
            raise
        log.info("Not found, creating %s/%s %s/%s", group, plural, namespace, name)
        custom_api.create_namespaced_custom_object(
            group,
            version,
            namespace,
            plural,
            body,
        )
        log.info("Created %s/%s %s/%s", group, plural, namespace, name)


def _ensure_ca_secret(core_api, namespace):
    """Copy the operators CA certificate secret into the target namespace.

    CNPG requires the CA secret (operators-ca-crt) to be present in the
    same namespace as the Cluster.  The canonical copy lives in the
    cert-manager namespace.  Always syncs from source to pick up rotations.
    """
    source = core_api.read_namespaced_secret(TLS_CA_SECRET, "cert-manager")
    body = kubernetes.client.V1Secret(
        metadata=kubernetes.client.V1ObjectMeta(
            name=TLS_CA_SECRET,
            namespace=namespace,
            labels={"cnpg.io/reload": ""},
        ),
        type=source.type,
        data=source.data,
    )
    try:
        core_api.replace_namespaced_secret(TLS_CA_SECRET, namespace, body)
    except ApiException as exc:
        if exc.status == 404:
            core_api.create_namespaced_secret(namespace, body)
            log.info("Copied %s to namespace %s", TLS_CA_SECRET, namespace)
        else:
            raise


def _ensure_backup_service_account(core_api, namespace, settings):
    annotations = {}
    if settings["provider"] == "s3":
        annotations["eks.amazonaws.com/role-arn"] = settings["irsa_role_arn"]

    body = kubernetes.client.V1ServiceAccount(
        metadata=kubernetes.client.V1ObjectMeta(
            name=settings["service_account_name"],
            namespace=namespace,
            annotations=annotations or None,
        )
    )

    try:
        core_api.replace_namespaced_service_account(
            settings["service_account_name"], namespace, body
        )
    except ApiException as exc:
        if exc.status == 404:
            core_api.create_namespaced_service_account(namespace, body)
            log.info(
                "Created backup ServiceAccount %s/%s",
                namespace,
                settings["service_account_name"],
            )
        else:
            raise


def _ensure_rustfs_secret(core_api, namespace, settings):
    source_secret = core_api.read_namespaced_secret(
        settings["rustfs_source_secret_name"],
        settings["rustfs_source_secret_namespace"],
    )

    copied_keys = ("access-key-id", "secret-key", "region")
    source_data = source_secret.data or {}
    missing_keys = [key for key in copied_keys if key not in source_data]
    if missing_keys:
        raise ValueError(
            "RustFS source secret is missing required keys: "
            + ", ".join(sorted(missing_keys))
        )

    body = kubernetes.client.V1Secret(
        metadata=kubernetes.client.V1ObjectMeta(
            name=settings["rustfs_secret_name"],
            namespace=namespace,
        ),
        type=source_secret.type,
        data={key: source_data[key] for key in copied_keys},
    )

    try:
        core_api.replace_namespaced_secret(
            settings["rustfs_secret_name"], namespace, body
        )
    except ApiException as exc:
        if exc.status == 404:
            core_api.create_namespaced_secret(namespace, body)
            log.info(
                "Copied RustFS backup secret to %s/%s",
                namespace,
                settings["rustfs_secret_name"],
            )
        else:
            raise


def _sync_barman_rolebinding_subject(
    rbac_api, namespace, cluster_name, service_account_name
):
    if rbac_api is None:
        return

    rolebinding_name = f"{cluster_name}-barman-cloud"
    try:
        rolebinding = rbac_api.read_namespaced_role_binding(rolebinding_name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            log.info(
                "Barman RoleBinding %s/%s not yet available for subject sync",
                namespace,
                rolebinding_name,
            )
            return
        raise

    desired_subject = {
        "kind": "ServiceAccount",
        "name": service_account_name,
        "namespace": namespace,
    }
    current_subjects = [
        {
            "kind": _field(subject, "kind"),
            "name": _field(subject, "name"),
            "namespace": _field(subject, "namespace"),
        }
        for subject in (_field(rolebinding, "subjects") or [])
    ]

    if current_subjects == [desired_subject]:
        return

    rbac_api.patch_namespaced_role_binding(
        rolebinding_name,
        namespace,
        {"subjects": [desired_subject]},
    )
    log.info(
        "Patched Barman RoleBinding %s/%s subject to ServiceAccount %s",
        namespace,
        rolebinding_name,
        service_account_name,
    )


def _delete_k8s_resource_quiet(fn, *args):
    """Call a K8s delete function, ignoring 404."""
    try:
        fn(*args)
    except ApiException as exc:
        if exc.status != 404:
            raise


def _zero_if_none(value):
    return value if value is not None else 0


def _field(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _find_condition(conditions, cond_type):
    for condition in conditions or []:
        if _field(condition, "type") == cond_type:
            return condition
    return None


def _condition_is_true(conditions, cond_type):
    condition = _find_condition(conditions, cond_type)
    return _field(condition, "status") == "True"


def _container_failure_reason(container_statuses):
    for container_status in container_statuses or []:
        state = _field(container_status, "state")
        waiting = _field(state, "waiting")
        if waiting is not None:
            reason = _field(waiting, "reason")
            if reason:
                return reason
        terminated = _field(state, "terminated")
        if terminated is not None:
            return _field(terminated, "reason") or "Terminated"
    return None


def _pod_is_ready(pod):
    if pod is None:
        return False
    if _field(_field(pod, "metadata"), "deletion_timestamp") is not None:
        return False
    if _field(_field(pod, "status"), "phase") != "Running":
        return False
    return _condition_is_true(_field(_field(pod, "status"), "conditions"), "Ready")


def _read_postgres_cluster_status(custom_api, namespace, name):
    try:
        cluster = custom_api.get_namespaced_custom_object(
            CNPG_GROUP,
            CNPG_VERSION,
            namespace,
            CNPG_PLURAL,
            name,
        )
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise
    return cluster.get("status") or {}


def _postgres_cluster_is_ready(status, expected_instances):
    if not status:
        return False
    return (
        _condition_is_true(status.get("conditions"), "Ready")
        and _zero_if_none(status.get("readyInstances")) >= expected_instances
        and bool(status.get("currentPrimary"))
    )


def _postgres_cluster_has_plugin(status, plugin_name):
    for plugin_status in status.get("pluginStatus") or []:
        if _field(plugin_status, "name") == plugin_name:
            return True
    return False


def _postgres_cluster_is_backup_ready(
    status, expected_instances, plugin_name, require_continuous_archiving=True
):
    if not _postgres_cluster_is_ready(status, expected_instances):
        return False
    if require_continuous_archiving and not _condition_is_true(
        status.get("conditions"), "ContinuousArchiving"
    ):
        return False
    return _postgres_cluster_has_plugin(status, plugin_name)


def _read_redis_cluster_status(custom_api, namespace, name):
    try:
        cluster = custom_api.get_namespaced_custom_object(
            REDIS_GROUP,
            REDIS_VERSION,
            namespace,
            REDIS_CLUSTER_PLURAL,
            name,
        )
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise
    return cluster.get("status") or {}


def _redis_cluster_health(status, expected_leader_replicas, expected_follower_replicas):
    if not status:
        return "provisioning", None

    state = str(status.get("state") or "")
    state_lower = state.lower()
    reason = status.get("reason")
    leader_replicas = _zero_if_none(status.get("readyLeaderReplicas"))
    follower_replicas = _zero_if_none(status.get("readyFollowerReplicas"))

    if (
        state_lower == "ready"
        and leader_replicas >= expected_leader_replicas
        and follower_replicas >= expected_follower_replicas
    ):
        return "ready", None
    if state_lower in {"error", "failed", "failure"}:
        return "error", reason or f"Redis operator reported state {state}"
    return "provisioning", None


def _read_redis_standalone_health(core_api, namespace, name):
    try:
        pod = core_api.read_namespaced_pod(f"{name}-0", namespace)
    except ApiException as exc:
        if exc.status == 404:
            return "provisioning", None
        raise

    if _pod_is_ready(pod):
        return "ready", None

    reason = _container_failure_reason(
        _field(_field(pod, "status"), "container_statuses") or []
    )
    if reason in {
        "CrashLoopBackOff",
        "Error",
        "ImagePullBackOff",
        "CreateContainerError",
    }:
        return "error", f"Redis pod is not healthy: {reason}"
    return "provisioning", None


# ---------------------------------------------------------------------------
# CNPG Cluster rendering
# ---------------------------------------------------------------------------


def _render_cnpg_cluster(resource, backup_settings=None):
    size = postgres_size_classes[resource.size_class]
    instances = 2 if resource.ha_enabled else 1
    name = _resource_k8s_name(resource)
    labels = _resource_labels(resource)

    pod_labels = _resource_pod_labels(resource)

    inherited_metadata = {
        "labels": pod_labels,
    }
    pod_annotations = _backing_service_pod_annotations()
    if pod_annotations:
        inherited_metadata["annotations"] = pod_annotations

    spec: dict[str, object] = {
        "instances": instances,
        "imageName": POSTGRES_IMAGES[resource.service_version],
        "inheritedMetadata": inherited_metadata,
        "certificates": {
            "serverTLSSecret": _tls_secret_name(resource),
            "serverCASecret": TLS_CA_SECRET,
        },
        "postgresql": {
            "parameters": resource.postgres_parameters or {},
        },
        "resources": {
            "requests": {
                "cpu": size["cpu"]["requests"],
                "memory": size["memory"]["requests"],
            },
            "limits": {
                "cpu": size["cpu"]["limits"],
                "memory": size["memory"]["limits"],
            },
        },
        "storage": {
            "size": f"{resource.storage_size}Gi",
        },
    }
    affinity = _cnpg_affinity(resource)
    if affinity:
        spec["affinity"] = affinity

    if backup_settings is None and _tenant_postgres_backups_enabled(resource):
        backup_settings = _tenant_postgres_backup_settings()

    if backup_settings:
        plugin = {
            "name": backup_settings["plugin_name"],
            "parameters": {
                "barmanObjectName": _backup_objectstore_name(resource),
                "serverName": name,
            },
        }
        if _postgres_backup_requires_continuous_archiving(resource):
            plugin["isWALArchiver"] = True
        spec["serviceAccountName"] = backup_settings["service_account_name"]
        spec["plugins"] = [plugin]

    cluster = {
        "apiVersion": f"{CNPG_GROUP}/{CNPG_VERSION}",
        "kind": "Cluster",
        "metadata": {
            "name": name,
            "labels": labels,
        },
        "spec": spec,
    }
    return cluster


# ---------------------------------------------------------------------------
# OpsTree Redis rendering
# ---------------------------------------------------------------------------


def _redis_spec_common(resource):
    """Return the common spec fields for both Redis and RedisCluster."""
    size = redis_size_classes[resource.size_class]
    spec = {
        "TLS": {
            "secret": {
                "optional": False,
                "secretName": _tls_secret_name(resource),
            },
        },
        "kubernetesConfig": {
            "image": REDIS_IMAGES[resource.service_version],
            "imagePullPolicy": "IfNotPresent",
            "redisSecret": {
                "key": "password",
                "name": _password_secret_name(resource),
            },
            "resources": {
                "requests": {
                    "cpu": size["cpu"]["requests"],
                    "memory": size["memory"]["requests"],
                },
                "limits": {
                    "cpu": size["cpu"]["limits"],
                    "memory": size["memory"]["limits"],
                },
            },
        },
        "podSecurityContext": {
            "fsGroup": 1000,
            "runAsUser": 1000,
        },
        "storage": {
            "volumeClaimTemplate": {
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {
                        "requests": {"storage": f"{resource.storage_size}Gi"},
                    },
                },
            },
        },
    }
    node_pool = _backing_services_pool()
    if node_pool:
        spec["nodeSelector"] = _node_selector_for_pool(node_pool)
        spec["tolerations"] = _tolerations_for_pool(node_pool)
    return spec


def _render_redis_standalone(resource):
    name = _resource_k8s_name(resource)
    spec = _redis_spec_common(resource)

    return {
        "apiVersion": f"{REDIS_GROUP}/{REDIS_VERSION}",
        "kind": "Redis",
        "metadata": {
            "name": name,
            "labels": _resource_pod_labels(resource),
        },
        "spec": spec,
    }


def _render_redis_cluster(resource):
    name = _resource_k8s_name(resource)
    spec = _redis_spec_common(resource)
    spec["clusterSize"] = max(resource.leader_replicas, resource.follower_replicas)
    spec["clusterVersion"] = f"v{resource.service_version}"
    spec["persistenceEnabled"] = True
    redis_leader = {"replicas": resource.leader_replicas}
    leader_affinity = _redis_role_affinity(resource, "leader", resource.leader_replicas)
    if leader_affinity:
        redis_leader["affinity"] = leader_affinity
    spec["redisLeader"] = redis_leader

    redis_follower = {"replicas": resource.follower_replicas}
    follower_affinity = _redis_role_affinity(
        resource, "follower", resource.follower_replicas
    )
    if follower_affinity:
        redis_follower["affinity"] = follower_affinity
    spec["redisFollower"] = redis_follower

    return {
        "apiVersion": f"{REDIS_GROUP}/{REDIS_VERSION}",
        "kind": "RedisCluster",
        "metadata": {
            "name": name,
            "labels": _resource_pod_labels(resource),
        },
        "spec": spec,
    }


# ---------------------------------------------------------------------------
# Per-resource reconcile functions
# ---------------------------------------------------------------------------


def _reconcile_postgres(resource, core_api, custom_api, apps_api=None, rbac_api=None):
    """Converge a single PostgresResource to its desired K8s state."""
    namespace = _resource_namespace(resource)
    name = _resource_k8s_name(resource)
    expected_instances = 2 if resource.ha_enabled else 1

    ensure_namespace(core_api, namespace)
    _ensure_ca_secret(core_api, namespace)

    backup_settings = None
    if _tenant_postgres_backups_enabled(resource):
        backup_settings = _tenant_postgres_backup_settings()
        _ensure_backup_service_account(core_api, namespace, backup_settings)
        if backup_settings["provider"] == "rustfs":
            _ensure_rustfs_secret(core_api, namespace, backup_settings)
        _ensure_custom_object(
            custom_api,
            BARMAN_GROUP,
            BARMAN_VERSION,
            namespace,
            BARMAN_OBJECT_STORE_PLURAL,
            _backup_objectstore_name(resource),
            _render_postgres_object_store(resource, backup_settings),
        )
    else:
        _delete_k8s_resource_quiet(
            custom_api.delete_namespaced_custom_object,
            CNPG_GROUP,
            CNPG_VERSION,
            namespace,
            CNPG_SCHEDULED_BACKUP_PLURAL,
            name,
        )
        _delete_k8s_resource_quiet(
            custom_api.delete_namespaced_custom_object,
            BARMAN_GROUP,
            BARMAN_VERSION,
            namespace,
            BARMAN_OBJECT_STORE_PLURAL,
            _backup_objectstore_name(resource),
        )

    cert_body = _render_postgres_certificate(resource)
    _ensure_certificate(custom_api, namespace, cert_body)

    body = _render_cnpg_cluster(resource, backup_settings=backup_settings)
    _ensure_custom_object(
        custom_api,
        CNPG_GROUP,
        CNPG_VERSION,
        namespace,
        CNPG_PLURAL,
        name,
        body,
    )
    if backup_settings:
        _sync_barman_rolebinding_subject(
            rbac_api,
            namespace,
            name,
            backup_settings["service_account_name"],
        )

    connection_info = {
        "host": f"{name}-rw.{namespace}.svc.cluster.local",
        "port": "5432",
        "dbname": "app",
        "username": "app",
        "sslmode": "verify-full",
        "secret_name": f"{name}-app",
    }
    _set_if_changed(resource, "connection_info", connection_info)

    status = _read_postgres_cluster_status(custom_api, namespace, name)

    # Read password from CNPG-generated secret (created by operator
    # once the cluster is healthy — may not exist yet)
    try:
        pg_secret = core_api.read_namespaced_secret(f"{name}-app", namespace)
        pg_password = base64.b64decode(pg_secret.data["password"]).decode()
        _sync_resource_env_configs(
            resource,
            _postgres_config_entries(resource, namespace, name, pg_password),
        )
    except ApiException as exc:
        if exc.status == 404:
            log.info(
                "CNPG app secret %s-app not yet available in %s, "
                "will retry on next reconcile",
                name,
                namespace,
            )
        else:
            raise

    if backup_settings and _postgres_cluster_is_backup_ready(
        status,
        expected_instances,
        backup_settings["plugin_name"],
        require_continuous_archiving=_postgres_backup_requires_continuous_archiving(
            resource
        ),
    ):
        _ensure_scheduled_backup(custom_api, namespace, resource, backup_settings)

    if _postgres_cluster_is_ready(status, expected_instances):
        _set_if_changed(resource, "provisioning_status", "ready")
        _set_if_changed(resource, "provisioning_error", None)
    else:
        _set_if_changed(resource, "provisioning_status", "provisioning")
        _set_if_changed(resource, "provisioning_error", None)


def _reconcile_redis(resource, core_api, custom_api, apps_api=None, rbac_api=None):
    """Converge a single RedisResource to its desired K8s state."""
    namespace = _resource_namespace(resource)
    name = _resource_k8s_name(resource)
    labels = _resource_labels(resource)

    ensure_namespace(core_api, namespace)

    cert_body = _render_redis_certificate(resource)
    _ensure_certificate(custom_api, namespace, cert_body)

    _ensure_password_secret(
        core_api,
        namespace,
        _password_secret_name(resource),
        labels,
    )

    if resource.ha_enabled:
        body = _render_redis_cluster(resource)
        plural = REDIS_CLUSTER_PLURAL
    else:
        body = _render_redis_standalone(resource)
        plural = REDIS_STANDALONE_PLURAL

    _ensure_custom_object(
        custom_api,
        REDIS_GROUP,
        REDIS_VERSION,
        namespace,
        plural,
        name,
        body,
    )
    if apps_api is not None:
        for statefulset_name in _redis_statefulset_names(resource, name):
            _sync_statefulset_pod_annotations(apps_api, namespace, statefulset_name)

    password_secret = core_api.read_namespaced_secret(
        _password_secret_name(resource), namespace
    )
    password = base64.b64decode(password_secret.data["password"]).decode()

    host = _redis_service_host(resource, namespace, name)
    connection_info = {
        "host": host,
        "port": "6379",
        "tls": True,
        "password_secret": _password_secret_name(resource),
    }
    if resource.ha_enabled:
        connection_info["client_mode"] = "cluster-aware"
        connection_info["startup_nodes"] = f"{host}:6379"
    _set_if_changed(resource, "connection_info", connection_info)
    _sync_resource_env_configs(
        resource, _redis_config_entries(resource, namespace, name, password)
    )

    if resource.ha_enabled:
        health_state, health_reason = _redis_cluster_health(
            _read_redis_cluster_status(custom_api, namespace, name),
            expected_leader_replicas=resource.leader_replicas,
            expected_follower_replicas=resource.follower_replicas,
        )
    else:
        health_state, health_reason = _read_redis_standalone_health(
            core_api,
            namespace,
            name,
        )

    _set_if_changed(resource, "provisioning_status", health_state)
    _set_if_changed(resource, "provisioning_error", health_reason)


def _delete_postgres(resource, core_api, custom_api, apps_api=None, rbac_api=None):
    """Remove all K8s objects for a deleted PostgresResource."""
    namespace = _resource_namespace(resource)
    name = _resource_k8s_name(resource)

    _delete_k8s_resource_quiet(
        custom_api.delete_namespaced_custom_object,
        CNPG_GROUP,
        CNPG_VERSION,
        namespace,
        CNPG_SCHEDULED_BACKUP_PLURAL,
        name,
    )
    _delete_k8s_resource_quiet(
        custom_api.delete_namespaced_custom_object,
        BARMAN_GROUP,
        BARMAN_VERSION,
        namespace,
        BARMAN_OBJECT_STORE_PLURAL,
        _backup_objectstore_name(resource),
    )
    _delete_k8s_resource_quiet(
        custom_api.delete_namespaced_custom_object,
        CNPG_GROUP,
        CNPG_VERSION,
        namespace,
        CNPG_PLURAL,
        name,
    )
    _delete_k8s_resource_quiet(
        custom_api.delete_namespaced_custom_object,
        CERTMANAGER_GROUP,
        CERTMANAGER_VERSION,
        namespace,
        CERTMANAGER_CERT_PLURAL,
        name,
    )
    _delete_k8s_resource_quiet(
        core_api.delete_namespaced_secret,
        _tls_secret_name(resource),
        namespace,
    )
    _cleanup_managed_env_configs(resource)
    log.info("Cleaned up PostgresResource %s (%s/%s)", resource.id, namespace, name)


def _delete_redis(resource, core_api, custom_api, apps_api=None, rbac_api=None):
    """Remove all K8s objects for a deleted RedisResource."""
    namespace = _resource_namespace(resource)
    name = _resource_k8s_name(resource)

    for plural in (REDIS_STANDALONE_PLURAL, REDIS_CLUSTER_PLURAL):
        _delete_k8s_resource_quiet(
            custom_api.delete_namespaced_custom_object,
            REDIS_GROUP,
            REDIS_VERSION,
            namespace,
            plural,
            name,
        )
    _delete_k8s_resource_quiet(
        custom_api.delete_namespaced_custom_object,
        CERTMANAGER_GROUP,
        CERTMANAGER_VERSION,
        namespace,
        CERTMANAGER_CERT_PLURAL,
        name,
    )
    _delete_k8s_resource_quiet(
        core_api.delete_namespaced_secret,
        _tls_secret_name(resource),
        namespace,
    )
    _delete_k8s_resource_quiet(
        core_api.delete_namespaced_secret,
        _password_secret_name(resource),
        namespace,
    )
    _cleanup_managed_env_configs(resource)
    log.info("Cleaned up RedisResource %s (%s/%s)", resource.id, namespace, name)


# ---------------------------------------------------------------------------
# Periodic reconcile task
# ---------------------------------------------------------------------------

_RECONCILERS = {
    "postgres": (_reconcile_postgres, _delete_postgres),
    "redis": (_reconcile_redis, _delete_redis),
}


@shared_task()
def reconcile_backing_services():
    """Periodic task: converge all backing service resources to desired state."""
    from cabotage.server.models.resources import Resource
    from cabotage.celery.tasks.build import (
        resume_branch_deploy_releases_for_environment,
    )

    lock_conn = _try_acquire_reconcile_lock()
    if lock_conn is None:
        log.info("Skipping backing-service reconcile; lock already held")
        return

    try:
        resources = Resource.query.filter(
            Resource.provisioning_status != "deleting",
            Resource.provisioning_status != "deleted",
        ).all()

        if not resources:
            return

        api_client = kubernetes_ext.kubernetes_client
        core_api = kubernetes.client.CoreV1Api(api_client)
        custom_api = kubernetes.client.CustomObjectsApi(api_client)
        apps_api = kubernetes.client.AppsV1Api(api_client)
        rbac_api = kubernetes.client.RbacAuthorizationV1Api(api_client)
        networking_api = kubernetes.client.NetworkingV1Api(api_client)

        for resource in resources:
            resource_type = resource.type
            resource_id = resource.id
            resource_slug = resource.slug

            entry = _RECONCILERS.get(resource_type)
            if entry is None:
                continue
            reconcile_fn, delete_fn = entry

            try:
                if resource.deleted_at is not None:
                    delete_fn(resource, core_api, custom_api, apps_api, rbac_api)
                    resource.provisioning_status = "deleted"
                    db.session.commit()
                else:
                    if not _backing_service_type_enabled(resource_type):
                        continue
                    namespace = _resource_namespace(resource)
                    ensure_namespace(core_api, namespace)
                    if (
                        has_app_context()
                        and current_app.config.get("NETWORK_POLICIES_ENABLED")
                        and namespace != "cabotage"
                    ):
                        ensure_network_policies(networking_api, namespace)
                    reconcile_fn(resource, core_api, custom_api, apps_api, rbac_api)
                    db.session.commit()
                    if resource.environment.forked_from_environment_id is not None:
                        resume_branch_deploy_releases_for_environment(
                            resource.environment_id
                        )
            except Exception:
                db.session.rollback()
                log.exception(
                    "Failed to reconcile %s resource %s (%s)",
                    resource_type,
                    resource_id,
                    resource_slug,
                )
                failed_resource = Resource.query.get(resource_id)
                if failed_resource is not None:
                    failed_resource.provisioning_status = "error"
                    failed_resource.provisioning_error = "Reconcile failed; will retry"
                    db.session.commit()
    finally:
        _release_reconcile_lock(lock_conn)


CA_CERT_PATH = "/var/run/secrets/cabotage.io/ca.crt"


def _postgres_config_entries(resource, namespace, name, password):
    """Build the (name, value, secret) tuples for a Postgres resource."""
    slug_upper = resource.slug.upper().replace("-", "_")
    host = f"{name}-rw.{namespace}.svc.cluster.local"
    return [
        (
            f"{slug_upper}_DATABASE_URL",
            f"postgresql://app:{password}@{host}:5432/app"
            f"?sslmode=verify-full&sslrootcert={CA_CERT_PATH}",
            True,
        ),
        (f"{slug_upper}_PGHOST", host, False),
        (f"{slug_upper}_PGPORT", "5432", False),
        (f"{slug_upper}_PGDATABASE", "app", False),
        (f"{slug_upper}_PGUSER", "app", False),
        (f"{slug_upper}_PGPASSWORD", password, True),
        (f"{slug_upper}_PGSSLMODE", "verify-full", False),
        (f"{slug_upper}_PGSSLROOTCERT", CA_CERT_PATH, False),
    ]


def _redis_service_host(resource, namespace, name):
    """Return the stable in-cluster Redis service hostname for this resource."""
    if resource.ha_enabled:
        service_name = f"{name}-master"
    else:
        service_name = name
    return f"{service_name}.{namespace}.svc.cluster.local"


def _redis_config_entries(resource, namespace, name, password):
    """Build the (name, value, secret) tuples for a Redis resource."""
    slug_upper = resource.slug.upper().replace("-", "_")
    host = _redis_service_host(resource, namespace, name)
    entries = [
        (
            f"{slug_upper}_REDIS_URL",
            f"rediss://:{password}@{host}:6379",
            True,
        ),
        (f"{slug_upper}_REDIS_HOST", host, False),
        (f"{slug_upper}_REDIS_PORT", "6379", False),
        (f"{slug_upper}_REDIS_PASSWORD", password, True),
        (f"{slug_upper}_REDIS_SSL_CA_CERTS", CA_CERT_PATH, False),
    ]
    if resource.ha_enabled:
        entries.extend(
            [
                (f"{slug_upper}_REDIS_CLUSTER", "true", False),
                (f"{slug_upper}_REDIS_STARTUP_NODES", f"{host}:6379", False),
            ]
        )
    return entries


def _sync_resource_env_configs(resource, entries):
    """Create or update EnvironmentConfiguration rows managed by a resource.

    entries: list of (name, value, secret) tuples.
    Existing configs for this resource are updated in place; missing ones
    are created; stale ones (not in entries) are deleted.
    """
    from cabotage.server.models.projects import EnvironmentConfiguration

    env = resource.environment
    project = env.project
    namespace = resource.environment.k8s_namespace
    prefix = project.k8s_identifier

    wanted_names = {name for name, _, _ in entries}
    managed_configs = {
        c.name: c
        for c in EnvironmentConfiguration.query.filter_by(resource_id=resource.id).all()
    }
    existing_by_name = {
        c.name: c
        for c in EnvironmentConfiguration.query.filter_by(
            project_id=project.id,
            environment_id=env.id,
        )
        .filter(EnvironmentConfiguration.name.in_(wanted_names))
        .all()
    }

    for name, value, secret in entries:
        config = existing_by_name.get(name) or managed_configs.get(name)
        if config is None:
            config = EnvironmentConfiguration(
                project_id=project.id,
                environment_id=env.id,
                resource_id=resource.id,
                name=name,
                value=value,
                secret=secret,
                buildtime=False,
            )
            db.session.add(config)
        else:
            config.resource_id = resource.id
            config.value = value
            config.secret = secret

        db.session.flush()

        try:
            key_slugs = config_writer.write_configuration(namespace, prefix, config)
            config.key_slug = key_slugs["config_key_slug"]
            config.build_key_slug = key_slugs["build_key_slug"]
            if config.secret:
                config.value = "**secure**"
        except Exception:
            log.warning(
                "Failed to write config %s to config_writer, storing direct",
                name,
                exc_info=True,
            )

    # Remove configs no longer in the wanted set
    for name, config in managed_configs.items():
        if name not in wanted_names:
            db.session.delete(config)

    db.session.flush()
    log.info("Synced %d env configs for resource %s", len(entries), resource.slug)


def _cleanup_managed_env_configs(resource):
    """Remove EnvironmentConfiguration rows managed by this resource."""
    from cabotage.server.models.projects import EnvironmentConfiguration

    configs = EnvironmentConfiguration.query.filter_by(resource_id=resource.id).all()
    for config in configs:
        db.session.delete(config)
    db.session.flush()


def _try_acquire_reconcile_lock():
    """Acquire the backing-service reconcile lock or return None if held."""
    conn = db.engine.connect()
    try:
        acquired = conn.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": _RECONCILE_LOCK_KEY},
        ).scalar()
        if not acquired:
            conn.close()
            return None
        return conn
    except Exception:
        conn.close()
        raise


def _acquire_reconcile_lock():
    """Acquire the backing-service reconcile lock, waiting if needed."""
    conn = db.engine.connect()
    try:
        conn.execute(
            text("SELECT pg_advisory_lock(:key)"),
            {"key": _RECONCILE_LOCK_KEY},
        )
        return conn
    except Exception:
        conn.close()
        raise


def _release_reconcile_lock(conn):
    """Release a previously-acquired backing-service reconcile lock."""
    try:
        conn.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": _RECONCILE_LOCK_KEY},
        )
    finally:
        conn.close()
