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


def deploy_release(release):
    api_client = kubernetes_ext.kubernetes_client
    core_api_instance = kubernetes.client.CoreV1Api(api_client)
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
