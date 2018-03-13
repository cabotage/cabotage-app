import time
import uuid

from base64 import b64encode

import kubernetes
import yaml

from kubernetes.client.rest import ApiException

from flask import current_app

from cabotage.server import (
    celery,
    kubernetes as kubernetes_ext,
)

from cabotage.server.models.projects import Release


class DeployError(RuntimeError):
    pass


def render_namespace(release):
    namespace_name = release.application.project.organization.slug
    namespace_object = kubernetes.client.V1Namespace(
        metadata=kubernetes.client.V1ObjectMeta(
            name=namespace_name,
        ),
    )
    return namespace_object


def create_namespace(core_api_instance, release):
    namespace_object = render_namespace(release)
    try:
        return core_api_instance.create_namespace(namespace_object)
    except Exception as exc:
        raise DeployError(f'Unexpected exception creating Namespace/{namespace_name}: {exc}')


def fetch_namespace(core_api_instance, release):
    namespace_name = release.application.project.organization.slug
    try:
        namespace = core_api_instance.read_namespace(namespace_name)
    except ApiException as exc:
        if exc.status == 404:
            namespace = create_namespace(core_api_instance, release)
        else:
            raise DeployError(f'Unexpected exception fetching Namespace/{namespace_name}: {exc}')
    return namespace


def render_service_account(release):
    service_account_name = f'{release.application.project.slug}-{release.application.slug}'
    service_account_object = kubernetes.client.V1ServiceAccount(
        metadata=kubernetes.client.V1ObjectMeta(
            name=service_account_name,
            labels={
                'org.pypi.infra.vault-access': 'true',
            },
        ),
    )
    return service_account_object


def create_service_account(core_api_instance, release):
    namespace = release.application.project.organization.slug
    service_account_object = render_service_account(release)
    try:
        return core_api_instance.create_namespaced_service_account(namespace, service_account_object)
    except Exception as exc:
        raise DeployError(f'Unexpected exception creating ServiceAccount/{service_account_name} in {namespace}: {exc}')


def fetch_service_account(core_api_instance, release):
    namespace = release.application.project.organization.slug
    service_account_name = f'{release.application.project.slug}-{release.application.slug}'
    try:
        service_account = core_api_instance.read_namespaced_service_account(service_account_name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            service_account = create_service_account(core_api_instance, release)
        else:
            raise DeployError(f'Unexpected exception fetching ServiceAccount/{service_account_name} in {namespace}: {exc}')
    return service_account


def render_image_pull_secrets(release):
    registry_auth_secret = current_app.config['REGISTRY_AUTH_SECRET']
    secret = kubernetes.client.V1Secret(
        type='kubernetes.io/dockerconfigjson',
        metadata=kubernetes.client.V1ObjectMeta(
            name=f'{release.application.project.slug}-{release.application.slug}',
        ),
        data={
            '.dockerconfigjson': b64encode(
                                     release.image_pull_secrets(
                                         registry_auth_secret,
                                         registry_urls=['localhost:30000'],
                                     ).encode()
                                 ).decode(),
        }
    )
    return secret

def create_image_pull_secret(core_api_instance, release):
    namespace = release.application.project.organization.slug
    secret_name = f'{release.application.project.slug}-{release.application.slug}'
    image_pull_secrets = render_image_pull_secrets(release)
    try:
        return core_api_instance.create_namespaced_secret(namespace, image_pull_secrets)
    except Exception as exc:
        raise DeployError(f'Unexpected exception creating Secret/{secret_name} in {namespace}: {exc}')


def fetch_image_pull_secrets(core_api_instance, release):
    namespace = release.application.project.organization.slug
    secret_name = f'{release.application.project.slug}-{release.application.slug}'
    try:
        secret = core_api_instance.read_namespaced_secret(secret_name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            secret = create_image_pull_secret(core_api_instance, release)
        else:
            raise DeployError(f'Unexpected exception fetching ServiceAccount/{secret_name} in {namespace}: {exc}')
    return secret

def render_cabotage_enroller_container(release, process_name, with_tls=True):
    role_name = f'{release.application.project.organization.slug}-{release.application.project.slug}-{release.application.slug}'

    args = [
        "kube_login",
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
        args.append(f"--service-names={process_name}.{release.application.slug}")

    return kubernetes.client.V1Container(
        name='cabotage-enroller',
        image='cabotage/sidecar:v1.0.0a1',
        image_pull_policy='IfNotPresent',
        env=[
            kubernetes.client.V1EnvVar(name='NAMESPACE', value_from=kubernetes.client.V1EnvVarSource(field_ref=kubernetes.client.V1ObjectFieldSelector(field_path='metadata.namespace'))),
            kubernetes.client.V1EnvVar(name='POD_NAME', value_from=kubernetes.client.V1EnvVarSource(field_ref=kubernetes.client.V1ObjectFieldSelector(field_path='metadata.name'))),
            kubernetes.client.V1EnvVar(name='POD_IP', value_from=kubernetes.client.V1EnvVarSource(field_ref=kubernetes.client.V1ObjectFieldSelector(field_path='status.podIP'))),
        ],
        args=args,
        volume_mounts=[
            kubernetes.client.V1VolumeMount(
                name='vault-secrets',
                mount_path='/var/run/secrets/vault'
            ),
        ],
    )

def render_cabotage_sidecar_container(release, with_tls=True):
    args = ["maintain"]
    if with_tls:
        args.append("--vault-pki-role={role_name}")
    role_name = f'{release.application.project.organization.slug}-{release.application.project.slug}-{release.application.slug}'
    return kubernetes.client.V1Container(
        name='cabotage-sidecar',
        image='cabotage/sidecar:v1.0.0a1',
        image_pull_policy='IfNotPresent',
        args=args,
        volume_mounts=[
            kubernetes.client.V1VolumeMount(
                name='vault-secrets',
                mount_path='/var/run/secrets/vault'
            ),
        ],
    )

def render_cabotage_sidecar_tls_container(release):
    role_name = f'{release.application.project.organization.slug}-{release.application.project.slug}-{release.application.slug}'
    return kubernetes.client.V1Container(
        name='cabotage-sidecar-tls',
        image='cabotage/sidecar:v1.0.0a1',
        image_pull_policy='IfNotPresent',
        command=["./ghostunnel"],
        args=[
            "server",
            "--keystore=/var/run/secrets/vault/combined.pem",
            "--cacert=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
            "--timed-reload=300s",
            "--shutdown-timeout=10s",
            "--connect-timeout=10s",
            "--disable-authentication",
            "--target=unix:///var/run/cabotage/cabotage.sock",
            "--listen=0.0.0.0:8000"
        ],
        volume_mounts=[
            kubernetes.client.V1VolumeMount(
                name='vault-secrets',
                mount_path='/var/run/secrets/vault'
            ),
            kubernetes.client.V1VolumeMount(
                name='cabotage-sock',
                mount_path='/var/run/cabotage'
            ),
        ],
        ports=[
            kubernetes.client.V1ContainerPort(
                protocol='TCP',
                name='tls',
                container_port=8000,
            ),
        ],
        liveness_probe=kubernetes.client.V1Probe(
            http_get=kubernetes.client.V1HTTPGetAction(
                scheme='HTTPS',
                port=8000,
                path='/_health/',
            ),
            initial_delay_seconds=5,
            period_seconds=1,
        ),
        readiness_probe=kubernetes.client.V1Probe(
            http_get=kubernetes.client.V1HTTPGetAction(
                scheme='HTTPS',
                port=8000,
                path='/_health/',
            ),
            initial_delay_seconds=5,
            period_seconds=1,
        ),
    )

def render_process_container(release, process_name, with_tls=True):
    volume_mounts = [
        kubernetes.client.V1VolumeMount(
            name='vault-secrets',
            mount_path='/var/run/secrets/vault'
        ),
    ]
    if with_tls:
        volume_mounts.append(
            kubernetes.client.V1VolumeMount(
                name='cabotage-sock',
                mount_path='/var/run/cabotage'
            )
        )
    return kubernetes.client.V1Container(
        name=process_name,
        image=f'localhost:30000/{release.repository_name}:release-{release.version}',
        image_pull_policy='Always',
        env=[
            kubernetes.client.V1EnvVar(name='VAULT_ADDR', value='https://vault.cabotage.svc.cluster.local'),
            kubernetes.client.V1EnvVar(name='VAULT_CACERT', value='/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'),
            kubernetes.client.V1EnvVar(name='CONSUL_HTTP_ADDR', value='https://consul.cabotage.svc.cluster.local:8443'),
            kubernetes.client.V1EnvVar(name='CONSUL_CACERT', value='/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'),
        ],
        args=[
            "envconsul",
            f"-config=/etc/cabotage/envconsul-{process_name}.hcl",
        ],
        resources=kubernetes.client.V1ResourceRequirements(
            limits={
                'memory': '1536Mi',
                'cpu': '1000m',
            },
            requests={
                'memory': '1024Mi',
                'cpu': '500m',
            },
        ),
        volume_mounts=volume_mounts,
    )


def render_podspec(release, process_name, service_account_name):
    if process_name.startswith('web'):
        return kubernetes.client.V1PodSpec(
            service_account_name=service_account_name,
            init_containers=[render_cabotage_enroller_container(release, process_name, with_tls=True)],
            containers=[
                render_cabotage_sidecar_container(release, with_tls=True),
                render_cabotage_sidecar_tls_container(release),
                render_process_container(release, process_name, with_tls=True),
            ],
            volumes=[
                kubernetes.client.V1Volume(
                    name='vault-secrets',
                    empty_dir=kubernetes.client.V1EmptyDirVolumeSource(medium="Memory", size_limit="1M")
                ),
                kubernetes.client.V1Volume(
                    name='cabotage-sock',
                    empty_dir=kubernetes.client.V1EmptyDirVolumeSource(medium="Memory", size_limit="1M")
                ),
            ],
        )
    elif process_name.startswith('worker'):
        return kubernetes.client.V1PodSpec(
            service_account_name=service_account_name,
            init_containers=[render_cabotage_enroller_container(release, process_name, with_tls=False)],
            containers=[
                render_cabotage_sidecar_container(release, with_tls=False),
                render_process_container(release, process_name, with_tls=False),
            ],
            volumes=[
                kubernetes.client.V1Volume(
                    name='vault-secrets',
                    empty_dir=kubernetes.client.V1EmptyDirVolumeSource(medium="Memory", size_limit="1M")
                ),
            ],
        )
    elif process_name.startswith('release'):
        return kubernetes.client.V1PodSpec(
            service_account_name=service_account_name,
            init_containers=[render_cabotage_enroller_container(release, process_name, with_tls=False)],
            containers=[
                render_process_container(release, process_name, with_tls=False),
            ],
            restart_policy='Never',
            volumes=[
                kubernetes.client.V1Volume(
                    name='vault-secrets',
                    empty_dir=kubernetes.client.V1EmptyDirVolumeSource(medium="Memory", size_limit="1M")
                ),
            ],
        )
    else:
        return kubernetes.client.V1PodSpec(
            service_account_name=service_account_name,
            init_containers=[render_cabotage_enroller_container(release, process_name, with_tls=False)],
            containers=[
                render_process_container(release, process_name, with_tls=False),
            ],
            volumes=[
                kubernetes.client.V1Volume(
                    name='vault-secrets',
                    empty_dir=kubernetes.client.V1EmptyDirVolumeSource(medium="Memory", size_limit="1M")
                ),
            ],
        )


def render_deployment(namespace, release, service_account_name, process_name):
    role_name = f'{release.application.project.organization.slug}-{release.application.project.slug}-{release.application.slug}'
    deployment_object = kubernetes.client.AppsV1beta1Deployment(
        metadata=kubernetes.client.V1ObjectMeta(
            name=f'{release.application.project.slug}-{release.application.slug}-{process_name}',
            labels={
                'organization': release.application.project.organization.slug,
                'project': release.application.project.slug,
                'application': release.application.slug,
                'process': process_name,
                'app': role_name,
            }
        ),
        spec=kubernetes.client.AppsV1beta1DeploymentSpec(
            replicas=release.application.process_counts.get(process_name, 0),
            selector=kubernetes.client.V1LabelSelector(
                match_labels={
                    'organization': release.application.project.organization.slug,
                    'project': release.application.project.slug,
                    'application': release.application.slug,
                    'process': process_name,
                    'app': role_name,
                },
            ),
            template=kubernetes.client.V1PodTemplateSpec(
                metadata=kubernetes.client.V1ObjectMeta(
                    labels={
                        'organization': release.application.project.organization.slug,
                        'project': release.application.project.slug,
                        'application': release.application.slug,
                        'process': process_name,
                        'app': role_name,
                    }
                ),
                spec=render_podspec(release, process_name, service_account_name),
            ),
        ),
    )
    return deployment_object


def create_deployment(apps_api_instance, namespace, release, service_account_name, process_name):
    deployment_object = render_deployment(namespace, release, service_account_name, process_name)
    deployment = None
    try:
        deployment = apps_api_instance.read_namespaced_deployment(deployment_object.metadata.name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            pass
        else:
            raise DeployError(f'Unexpected exception fetching Deployment/{deployment_object.metadata.name} in {namespace}: {exc}')
    if deployment is None:
        try:
            return apps_api_instance.create_namespaced_deployment(namespace, deployment_object)
        except Exception as exc:
            raise DeployError(f'Unexpected exception creating Deployment/{deployment_object.metadata.name} in {namespace}: {exc}')
    else:
        try:
            return apps_api_instance.patch_namespaced_deployment(deployment_object.metadata.name, namespace, deployment_object)
        except Exception as exc:
            raise DeployError(f'Unexpected exception patching Deployment/{deployment_object.metadata.name} in {namespace}: {exc}')


def scale_deployment(namespace, release, process_name, replicas):
    api_client = kubernetes_ext.kubernetes_client
    apps_api_instance = kubernetes.client.AppsV1beta1Api(api_client)
    deployment_name = f'{release.application.project.slug}-{release.application.slug}-{process_name}'
    deployment = None
    try:
        deployment = apps_api_instance.read_namespaced_deployment(deployment_name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            raise DeployError(f'Unexpected exception fetching Deployment/{deployment_name} in {namespace}: {exc}')
    if deployment is not None:
        scale = kubernetes.client.AppsV1beta1Scale(
            spec=kubernetes.client.AppsV1beta1ScaleSpec(replicas=replicas)
        )
        api_response = apps_api_instance.patch_namespaced_deployment_scale(deployment_name, namespace, scale)


def render_job(namespace, release, service_account_name, process_name):
    role_name = f'{release.application.project.organization.slug}-{release.application.project.slug}-{release.application.slug}'
    job_id = str(uuid.uuid4())
    job_object = kubernetes.client.V1Job(
        metadata=kubernetes.client.V1ObjectMeta(
            name=f'{release.application.project.slug}-{release.application.slug}-{process_name}-{job_id}',
            labels={
                'organization': release.application.project.organization.slug,
                'project': release.application.project.slug,
                'application': release.application.slug,
                'process': process_name,
                'app': role_name,
                'release': str(release.version),
                'deployment': job_id,
            }
        ),
        spec=kubernetes.client.V1JobSpec(
            active_deadline_seconds=120,
            backoff_limit=0,
            parallelism=1,
            template=kubernetes.client.V1PodTemplateSpec(
                metadata=kubernetes.client.V1ObjectMeta(
                    labels={
                        'organization': release.application.project.organization.slug,
                        'project': release.application.project.slug,
                        'application': release.application.slug,
                        'process': process_name,
                        'app': role_name,
                        'release': str(release.version),
                        'deployment': job_id,
                    }
                ),
                spec=render_podspec(release, process_name, service_account_name),
            ),
        ),
    )
    return job_object


def run_job(batch_api_instance, namespace, release, service_account_name, process_name):
    job_object = render_job(namespace, release, service_account_name, process_name)
    try:
        job = batch_api_instance.create_namespaced_job(namespace, job_object)
    except ApiException as exc:
        raise DeployError(f'Unexpected exception creating Job/{job_object.metadata.name} in {namespace}: {exc}')
    while True:
        job_status = batch_api_instance.read_namespaced_job_status(job_object.metadata.name, namespace)
        if job_status.status.failed and job.status.failed > 0:
            raise DeployError(f'Job/{job_object.metadata.name} failed!')
        elif job_status.status.succeeded and job_status.status.succeeded > 0:
            try:
                status = batch_api_instance.delete_namespaced_job(
                    job_object.metadata.name, namespace,
                    kubernetes.client.V1DeleteOptions(
                        propagation_policy='Foreground',
                    )
                )
            except ApiException as exc:
                raise DeployError(f'Unexpected exception deleting Job/{job_object.metadata.name} in {namespace}: {exc}')
            return True
        else:
            time.sleep(1)


def deploy_release(release):
    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)
    apps_api_instance = kubernetes.client.AppsV1beta1Api(api_client)
    batch_api_instance = kubernetes.client.BatchV1Api(api_client)
    namespace = fetch_namespace(core_api_instance, release)
    service_account = fetch_service_account(core_api_instance, release)
    image_pull_secrets = fetch_image_pull_secrets(core_api_instance, release)
    service_account = core_api_instance.patch_namespaced_service_account(
        service_account.metadata.name,
        namespace.metadata.name,
        kubernetes.client.V1ServiceAccount(
            image_pull_secrets=[kubernetes.client.V1LocalObjectReference(name=image_pull_secrets.metadata.name)],
        ),
    )
    for release_command in release.release_commands:
        job = run_job(batch_api_instance, namespace.metadata.name, release, service_account.metadata.name, release_command)
    for process_name in release.processes:
        deployment = create_deployment(apps_api_instance, namespace.metadata.name, release, service_account.metadata.name, process_name)


def remove_none(obj):
    if isinstance(obj, (list, tuple, set)):
        return type(obj)(remove_none(x) for x in obj if x is not None)
    elif isinstance(obj, dict):
        return type(obj)((remove_none(k), remove_none(v))
            for k, v in obj.items() if k is not None and v is not None)
    else:
        return obj


def render_release(release):
    namespace = render_namespace(release)
    service_account = render_service_account(release)
    image_pull_secrets = render_image_pull_secrets(release)
    document = {
      'namespace': namespace.to_dict(),
      'service_account': service_account.to_dict(),
      'image_pull_secrets': image_pull_secrets.to_dict(),
      'deployments': [
          render_deployment(namespace.metadata.name, release, service_account.metadata.name, process_name).to_dict()
          for process_name in release.processes
      ],
      'release_jobs': [
          render_job(namespace.metadata.name, release, service_account.metadata.name, process_name).to_dict()
          for process_name in release.release_commands
      ],
    }
    document = remove_none(document)
    return yaml.dump(document, default_flow_style=False)


@celery.task()
def run_deploy_release(release_id=None):
    release = Release.query.filter_by(id=release_id).first()
    if release is None:
        raise KeyError(f'Release with ID {release_id} not found!')
    error = False
    error_detail = ""
    try:
        deploy_release(release)
    except DeployError as exc:
        error = True
        error_detail = str(exc)
        print(error_detail)
        print(exc)
    except Exception:
        raise
