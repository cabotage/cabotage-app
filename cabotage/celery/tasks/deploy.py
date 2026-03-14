import secrets
import time

from base64 import b64encode

import kubernetes
import yaml

from celery import shared_task
from kubernetes.client.rest import ApiException

from flask import current_app

from cabotage.server import (
    config_writer,
    db,
    github_app,
    kubernetes as kubernetes_ext,
)

from cabotage.server.models.projects import (
    Deployment,
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


class DeployError(RuntimeError):
    pass


def _preview_url_for_app_env(app_env):
    """Return the https:// URL for the first auto-generated ingress host, or None."""
    for ingress in app_env.ingresses:
        if not ingress.enabled:
            continue
        for host in ingress.hosts:
            if host.is_auto_generated and host.tls_enabled:
                return f"https://{host.hostname}"
    return None


def _wait_for_tls_secret(core_api, namespace, secret_name, timeout=120, log=None):
    """Poll until a TLS secret exists with cert data, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            secret = core_api.read_namespaced_secret(secret_name, namespace)
            if secret.data and "tls.crt" in secret.data:
                if log:
                    log(f"TLS secret {secret_name} is ready")
                return True
        except ApiException as exc:
            if exc.status != 404:
                raise
        time.sleep(5)
    if log:
        log(f"TLS secret {secret_name} not ready after {timeout}s, proceeding anyway")
    return False


def k8s_namespace(release):
    org_k8s = release.application.project.organization.k8s_identifier
    app_env = release.application_environment
    if app_env.k8s_identifier is not None:
        return safe_k8s_name(org_k8s, app_env.environment.k8s_identifier)
    return org_k8s


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
    if app_env.k8s_identifier is not None:
        pairs.append((app_env.environment.slug, app_env.environment.k8s_identifier))
    pairs.append((project.slug, project.k8s_identifier))
    pairs.append((app.slug, app.k8s_identifier))
    return compact_k8s_name(*pairs)


def render_namespace(release):
    namespace_object = kubernetes.client.V1Namespace(
        metadata=kubernetes.client.V1ObjectMeta(
            name=k8s_namespace(release),
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


def fetch_namespace(core_api_instance, release):
    namespace_name = k8s_namespace(release)
    try:
        namespace = core_api_instance.read_namespace(namespace_name)
    except ApiException as exc:
        if exc.status == 404:
            namespace = create_namespace(core_api_instance, release)
        else:
            raise DeployError(
                f"Unexpected exception fetching Namespace/{namespace_name}: {exc}"
            )
    return namespace


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
    env = release.application_environment.environment
    forked_from = env.forked_from_environment
    if forked_from:
        org_k8s = release.application.project.organization.k8s_identifier
        base_ns = safe_k8s_name(org_k8s, forked_from.k8s_identifier)
        cabotage_enrollment_object["spec"] = {
            "inheritsFrom": [
                {
                    "namespace": base_ns,
                    "name": cabotage_enrollment_name,
                }
            ],
        }
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


def render_service(release, process_name):
    resource_prefix = k8s_resource_prefix(release)
    service_name = f"{resource_prefix}-{process_name}"
    label_value = k8s_label_value(release)
    service_object = kubernetes.client.V1Service(
        metadata=kubernetes.client.V1ObjectMeta(
            name=service_name,
            labels={
                "resident-service.cabotage.io": "true",
                "app": resource_prefix,
                "process": process_name,
            },
        ),
        spec=kubernetes.client.V1ServiceSpec(
            ports=[
                kubernetes.client.V1ServicePort(
                    port=8000,
                    target_port=8000,
                )
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
    service_name = f"{k8s_resource_prefix(release)}-{process_name}"
    try:
        service = core_api_instance.read_namespaced_service(service_name, namespace)
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
    """Build a V1Ingress from an Ingress model record."""
    if not ingress.enabled:
        return None

    resource_prefix = k8s_resource_prefix(release)
    ingress_name = f"{resource_prefix}-{ingress.name}"
    label_value = k8s_label_value(release)

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

    # Build paths once — shared across all hosts
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
                        port=kubernetes.client.V1ServiceBackendPort(
                            number=8000,
                        ),
                    ),
                ),
            )
        )

    if not k8s_paths:
        # No explicit paths — default to "/" routing to first web process
        web_procs = sorted(p for p in release.processes if p.startswith("web"))
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
                        port=kubernetes.client.V1ServiceBackendPort(
                            number=8000,
                        ),
                    ),
                ),
            )
        )

    tls_hosts = []
    rules = []
    for host in ingress.hosts:
        if host.tls_enabled:
            tls_hosts.append(host.hostname)
        rules.append(
            kubernetes.client.V1IngressRule(
                host=host.hostname,
                http=kubernetes.client.V1HTTPIngressRuleValue(paths=k8s_paths),
            )
        )

    tls = []
    if tls_hosts:
        tls.append(
            kubernetes.client.V1IngressTLS(
                hosts=tls_hosts,
                secret_name=f"{ingress_name}-tls",
            )
        )

    ingress_object = kubernetes.client.V1Ingress(
        metadata=kubernetes.client.V1ObjectMeta(
            name=ingress_name,
            labels={
                "resident-ingress.cabotage.io": "true",
                "organization": release.application.project.organization.slug,
                "project": release.application.project.slug,
                "application": release.application.slug,
                "app": label_value,
                "ingress": ingress.name,
            },
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
            "Unexpected exception fetching Ingress/" f"{k8s_name} in {namespace}: {exc}"
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


def render_cabotage_enroller_container(release, process_name, with_tls=True):
    role_name = k8s_role_name(release)
    resource_prefix = k8s_resource_prefix(release)

    args = [
        "kube-login",
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
        name="cabotage-enroller",
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
        volume_mounts=[
            kubernetes.client.V1VolumeMount(
                name="vault-secrets", mount_path="/var/run/secrets/vault"
            ),
        ],
    )


def render_cabotage_sidecar_container(release, with_tls=True):
    role_name = k8s_role_name(release)
    args = ["maintain"]
    if with_tls:
        args.append(f"--vault-pki-role={role_name}")
    return kubernetes.client.V1Container(
        name="cabotage-sidecar",
        restart_policy="Always",
        image=current_app.config["SIDECAR_IMAGE"],
        image_pull_policy="IfNotPresent",
        args=args,
        volume_mounts=[
            kubernetes.client.V1VolumeMount(
                name="vault-secrets", mount_path="/var/run/secrets/vault"
            ),
        ],
        resources=kubernetes.client.V1ResourceRequirements(
            limits={
                "memory": "48Mi",
                "cpu": "20m",
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
        image=f'{current_app.config["REGISTRY_PULL"]}/{release.repository_name}:release-{release.version}',
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
                name="DD_CONFD_PATH", value="/tmp/null"  # nosec
            ),
            kubernetes.client.V1EnvVar(
                name="DD_AUTOCONF_TEMPLATE_DIR", value="/tmp/null"  # nosec
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
            render_cabotage_enroller_container(release, process_name, with_tls=True)
        )
        init_containers.append(
            render_cabotage_sidecar_container(release, with_tls=True)
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
            render_cabotage_enroller_container(release, process_name, with_tls=True)
        )
        init_containers.append(
            render_cabotage_sidecar_container(release, with_tls=True)
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
            render_cabotage_enroller_container(release, process_name, with_tls=False)
        )
        init_containers.append(
            render_cabotage_sidecar_container(release, with_tls=False)
        )
        containers.append(
            render_process_container(
                release, process_name, datadog_tags, with_tls=False, unix=False
            )
        )
    elif process_name.startswith("release"):
        init_containers.append(
            render_cabotage_enroller_container(release, process_name, with_tls=False)
        )
        containers.append(
            render_process_container(
                release, process_name, datadog_tags, with_tls=False, unix=False
            )
        )
        restart_policy = "Never"
    elif process_name.startswith("postdeploy"):
        init_containers.append(
            render_cabotage_enroller_container(release, process_name, with_tls=False)
        )
        containers.append(
            render_process_container(
                release, process_name, datadog_tags, with_tls=False, unix=False
            )
        )
        restart_policy = "Never"
    else:
        init_containers.append(
            render_cabotage_enroller_container(release, process_name, with_tls=False)
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

    return kubernetes.client.V1PodSpec(
        service_account_name=service_account_name,
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
            return apps_api_instance.patch_namespaced_deployment(
                deployment_object.metadata.name,
                namespace,
                deployment_object,
                field_validation="Ignore",
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
                    }
                ),
                spec=render_podspec(release, process_name, service_account_name),
            ),
        ),
    )
    return job_object


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
            job_status = batch_api_instance.read_namespaced_job_status(
                job_object.metadata.name, namespace
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
            job_status = batch_api_instance.read_namespaced_job_status(
                job_name, namespace
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
            except Exception:  # nosec B110
                pass

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
        log("Fetching Cabotage CA Cert ConfigMap")
        fetch_cabotage_ca_configmap(core_api_instance, deployment.release_object)
        log("Fetching ServiceAccount")
        service_account = fetch_service_account(
            core_api_instance, deployment.release_object
        )
        log("Fetching CabotageEnrollment")
        fetch_cabotage_enrollment(
            custom_objects_api_instance, deployment.release_object
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
        if current_app.config.get("INGRESS_DOMAIN"):
            ingress_domain = current_app.config["INGRESS_DOMAIN"]
            app_env = deployment.application_environment
            hostname_pairs = _ingress_hostname_pairs(app_env)
            changed = False
            for ing in app_env.ingresses:
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
            for ingress in ingress_snapshots:
                log(f"Fetching Ingress/{ingress.name}")
                fetch_ingress(
                    networking_api_instance,
                    release_obj,
                    ingress,
                )
            cleanup_orphaned_ingresses(
                networking_api_instance,
                release_obj,
                [i.name for i in ingress_snapshots],
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
        # Commit the session before running release commands (e.g., migrations)
        # to release any row/table locks. Without this, migrations that ALTER
        # tables read by this task will deadlock — the migration waits for our
        # lock, and we wait for the migration to finish.
        db.session.commit()
        db.session.refresh(deployment)
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
            except Exception:  # nosec B110
                pass
        deployment.deploy_log = "\n".join(deploy_log)
        db.session.commit()
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
            except Exception:  # nosec B110
                pass
        deployment.deploy_log = "\n".join(deploy_log)
        db.session.commit()
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
        except Exception:  # nosec B110
            pass
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
                tls_secret = f"{resource_prefix}-{ing.name}-tls"
                log(f"Waiting for TLS certificate ({tls_secret})")
                tls_ready = _wait_for_tls_secret(
                    core_api_instance,
                    namespace.metadata.name,
                    tls_secret,
                    log=log,
                )
                break
        if not tls_ready:
            env_url = None

    if (
        deployment.deploy_metadata
        and "installation_id" in deployment.deploy_metadata
        and "statuses_url" in deployment.deploy_metadata
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
    if current_app.config.get("INGRESS_DOMAIN"):
        ingress_domain = current_app.config["INGRESS_DOMAIN"]
        app_env = deployment.application_environment
        hostname_pairs = _ingress_hostname_pairs(app_env)
        changed = False
        for ing in app_env.ingresses:
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
    deployment.deploy_log = "\n".join(deploy_log)
    db.session.commit()
    if (
        deployment.deploy_metadata
        and "installation_id" in deployment.deploy_metadata
        and "statuses_url" in deployment.deploy_metadata
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
