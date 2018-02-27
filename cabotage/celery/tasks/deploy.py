from base64 import b64encode

import kubernetes
from kubernetes.client.rest import ApiException

from flask import current_app

from cabotage.server import (
    celery,
    kubernetes as kubernetes_ext,
)

from cabotage.server.models.projects import Release


class DeployError(RuntimeError):
    pass


def create_namespace(core_api_instance, release):
    namespace_name = release.application.project.organization.slug
    namespace_object = kubernetes.client.V1Namespace(
        metadata=kubernetes.client.V1ObjectMeta(
            name=namespace_name,
        ),
    )
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


def create_service_account(core_api_instance, release):
    namespace = release.application.project.organization.slug
    service_account_name = f'{release.application.project.slug}-{release.application.slug}'
    service_account_object = kubernetes.client.V1ServiceAccount(
        metadata=kubernetes.client.V1ObjectMeta(
            name=service_account_name,
            labels={
                'org.pypi.infra.vault-access': 'true',
            },
        ),
    )
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


def _image_pull_secrets(release):
    registry_auth_secret = current_app.config['CABOTAGE_REGISTRY_AUTH_SECRET']
    secret = kubernetes.client.V1Secret(
        type='kubernetes.io/dockerconfigjson',
        metadata=kubernetes.client.V1ObjectMeta(
            name=f'{release.application.project.slug}-{release.application.slug}',
        ),
        data={
            '.dockerconfigjson': b64encode(
                                     release.image_pull_secrets(
                                         registry_auth_secret,
                                         registry_urls=['localhost:5000'],
                                     ).encode()
                                 ).decode(),
        }
    )
    return secret

def create_image_pull_secret(core_api_instance, release):
    namespace = release.application.project.organization.slug
    secret_name = f'{release.application.project.slug}-{release.application.slug}'
    try:
        print(_image_pull_secrets(release))
        return core_api_instance.create_namespaced_secret(namespace, _image_pull_secrets(release))
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

def create_cabotage_enroller_container(release, process_name):
    role_name = f'{release.application.project.organization.slug}-{release.application.project.slug}-{release.application.slug}'
    return kubernetes.client.V1Container(
        name='cabotage-enroller',
        image='gcr.io/the-psf/cabotage-sidecar:v1.0.0a1',
        image_pull_policy='IfNotPresent',
        env=[
            kubernetes.client.V1EnvVar(name='NAMESPACE', value_from=kubernetes.client.V1EnvVarSource(field_ref=kubernetes.client.V1ObjectFieldSelector(field_path='metadata.namespace'))),
            kubernetes.client.V1EnvVar(name='POD_NAME', value_from=kubernetes.client.V1EnvVarSource(field_ref=kubernetes.client.V1ObjectFieldSelector(field_path='metadata.name'))),
            kubernetes.client.V1EnvVar(name='POD_IP', value_from=kubernetes.client.V1EnvVarSource(field_ref=kubernetes.client.V1ObjectFieldSelector(field_path='status.podIP'))),
        ],
        args=[
            "kube_login",
            "--namespace=$(NAMESPACE)",
            f"--vault-auth-kubernetes-role={role_name}",
            "--fetch-cert",
            f"--vault-pki-role={role_name}",
            "--fetch-consul-token",
            f"--vault-consul-role={role_name}",
            "--pod-name=$(POD_NAME)",
            "--pod-ip=$(POD_IP)",
            f"--service-names={process_name}.{release.application.slug}",
        ],
        volume_mounts=[
            kubernetes.client.V1VolumeMount(
                name='vault-secrets',
                mount_path='/var/run/secrets/vault'
            ),
        ],
    )

def create_cabotage_sidecar_container(release):
    role_name = f'{release.application.project.organization.slug}-{release.application.project.slug}-{release.application.slug}'
    return kubernetes.client.V1Container(
        name='cabotage-sidecar',
        image='gcr.io/the-psf/cabotage-sidecar:v1.0.0a1',
        image_pull_policy='IfNotPresent',
        args=[
            "maintain",
            f"--vault-pki-role={role_name}",
        ],
        volume_mounts=[
            kubernetes.client.V1VolumeMount(
                name='vault-secrets',
                mount_path='/var/run/secrets/vault'
            ),
        ],
    )

def create_process_container(release, process_name):
    return kubernetes.client.V1Container(
        name=process_name,
        image=f'localhost:5000/{release.repository_name}:release-{release.version}',
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
                'memory': '128Mi',
                'cpu': '100m',
            },
            requests={
                'memory': '64Mi',
                'cpu': '50m',
            },
        ),
        volume_mounts=[
            kubernetes.client.V1VolumeMount(
                name='vault-secrets',
                mount_path='/var/run/secrets/vault'
            ),
        ],
    )

def create_deployment(apps_api_instance, namespace, release, service_account_name, process_name):
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
            replicas=1,
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
                spec=kubernetes.client.V1PodSpec(
                    service_account_name=service_account_name,
                    init_containers=[create_cabotage_enroller_container(release, process_name)],
                    containers=[create_cabotage_sidecar_container(release), create_process_container(release, process_name)],
                    volumes=[
                        kubernetes.client.V1Volume(
                            name='vault-secrets',
                            empty_dir=kubernetes.client.V1EmptyDirVolumeSource(medium="Memory", size_limit="1M")
                        ),
                    ],
                ),
            ),
        ),
    )
    return apps_api_instance.create_namespaced_deployment(namespace, deployment_object)


def deploy_release(release):
    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)
    apps_api_instance = kubernetes.client.AppsV1beta1Api(api_client)
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
    deployment = create_deployment(apps_api_instance, namespace.metadata.name, release, service_account.metadata.name, 'web')


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
