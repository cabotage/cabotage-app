import datetime
import gzip
import io
import json
import os
import shutil
import stat
import sys

from celery import shared_task
from contextlib import ExitStack
from tarfile import (
    TarFile,
    TarError,
)
from tempfile import (
    TemporaryDirectory,
    TemporaryFile,
)

import docker
import docker.tls
import procfile

from dockerfile_parse import DockerfileParser
from flask import current_app

from cabotage.celery.tasks.deploy import run_deploy

from cabotage.server import (
    db,
    github_app,
    minio,
    config_writer,
)

from cabotage.server.models.projects import (
    activity_plugin,
    Image,
    Release,
    Deployment,
)

from cabotage.utils.docker_auth import (
    check_docker_credentials,
    generate_docker_credentials,
    generate_docker_registry_jwt,
    parse_docker_scope,
    docker_access_intersection,
)

from cabotage.utils.github import post_deployment_status_update

Activity = activity_plugin.activity_cls


class BuildError(RuntimeError):
    pass


RELEASE_DOCKERFILE_TEMPLATE = """
FROM {registry}/{image.repository_name}:image-{image.version}

"""

ENTRYPOINT = """#!/bin/sh

export VAULT_TOKEN=$(cat /var/run/secrets/vault/vault-token)
export CONSUL_TOKEN=$(cat /var/run/secrets/vault/consul-token)

exec "${@}"
"""


def build_release(release,
                  registry, registry_username, registry_password,
                  docker_url, docker_secure, docker_ca):
    with ExitStack() as stack:
        temp_dir = stack.enter_context(TemporaryDirectory())
        with open(os.path.join(temp_dir, 'entrypoint.sh'), 'w') as fd:
            fd.write(ENTRYPOINT)
        st = os.stat(os.path.join(temp_dir, 'entrypoint.sh'))
        os.chmod(os.path.join(temp_dir, 'entrypoint.sh'), st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        with open(os.path.join(temp_dir, 'Dockerfile'), 'a') as fd:
            fd.write(RELEASE_DOCKERFILE_TEMPLATE.format(registry=registry, image=release.image_object))
            fd.write(f'COPY envconsul-linux-amd64 /usr/bin/envconsul\n')
            fd.write(f'COPY entrypoint.sh /entrypoint.sh\n')
            for process_name in  release.envconsul_configurations:
                fd.write(f'COPY envconsul-{process_name}.hcl /etc/cabotage/envconsul-{process_name}.hcl\n')
            fd.write(f'USER nobody\n')
            fd.write(f'ENTRYPOINT ["/entrypoint.sh"]\n')
            fd.write(f'CMD []\n')
        with open(os.path.join(temp_dir, 'Dockerfile'), 'r') as release_dockerfile:
            dockerfile_body = release_dockerfile.read()
        release.dockerfile = dockerfile_body
        db.session.commit()
        shutil.copy(
            'envconsul-linux-amd64',
            os.path.join(temp_dir, 'envconsul-linux-amd64'),
        )
        for process_name, envconsul_configuration in  release.envconsul_configurations.items():
            with open(os.path.join(temp_dir, f'envconsul-{process_name}.hcl'), 'w') as envconsul_config:
                envconsul_config.write(envconsul_configuration)
        tls_config = False
        if docker_secure:
            tls_config = docker.tls.TLSConfig(client_cert=None, ca_cert=docker_ca, verify=True, ssl_version='PROTOCOL_TLSv1_2')
        client = docker.DockerClient(base_url=docker_url, tls=tls_config)
        client.login(
            username=registry_username,
            password=registry_password,
            registry=registry,
            reauth=True,
        )
        response = client.api.build(
            path=temp_dir,
            tag=f'{registry}/{release.repository_name}:release-{release.version}',
            rm=True,
            forcerm=True,
            dockerfile="Dockerfile",
        )
        build_errored = False
        log_lines = []
        for chunk in response:
            for line in chunk.split(b'\r\n'):
                if line:
                    payload = json.loads(line.decode())
                    stream = payload.get('stream')
                    status = payload.get('status')
                    aux = payload.get('aux')
                    error = payload.get('error')
                    if stream:
                        log_lines.append(stream)
                    if status:
                        if payload.get('progressDetail'):
                            continue
                        if payload.get("id"):
                          log_lines.append(f'{payload.get("id")}: {status}\n')
                        else:
                          log_lines.append(f'{status}\n')
                    if error is not None:
                        errorDetail = payload.get('errorDetail', {})
                        message = errorDetail.get('message', 'unknown error')
                        build_errored = (
                            f'Error building release: {message}'
                        )
                    if aux:
                        if 'ID' in aux:
                            built_id = aux['ID']
                            log_lines.append(f'Built Image with ID: {built_id}\n')
        release.release_build_log = ''.join(log_lines)
        db.session.commit()
        if build_errored:
            raise BuildError(build_errored)
        built_release = client.images.get(
            f'{registry}/{release.repository_name}:release-{release.version}'
        )
        client.images.push(
            f'{registry}/{release.repository_name}',
            f'release-{release.version}'
        )
        pushed_release = client.images.get(
            f'{registry}/{release.repository_name}:release-{release.version}'
        )
        return {
            'release_id': pushed_release.id,
            'dockerfile': dockerfile_body,
        }


def build_image(tarfileobj, image,
                registry, registry_username, registry_password,
                docker_url, docker_secure, docker_ca):
    with ExitStack() as stack:
        temp_dir = stack.enter_context(TemporaryDirectory())
        try:
            tar_ball = stack.enter_context(TarFile(fileobj=tarfileobj, mode='r'))
        except Exception as exc:
            raise BuildError(f'{exc}')
        for tarinfo in tar_ball:
            if os.path.normpath(tarinfo.name).startswith((os.sep, '/', '..')):
                raise BuildError(
                    ('refusing to touch sketchy tarball, '
                     'no relative paths outside of root directory allowed '
                     f'{tarinfo.name} exits top level directory')
                )
            if not (tarinfo.isfile() or tarinfo.isdir()):
                raise BuildError(
                    ('refusing to touch sketchy tarball, '
                     'only regular files and directories allowed '
                     f'{tarinfo.name} is not a regular file or directory')
                )
        try:
            try:
                tar_ball.getmember('./Dockerfile.cabotage')
            except KeyError:
                tar_ball.getmember('./Dockerfile')
        except KeyError:
            raise BuildError(
                ('must include a Dockerfile.cabotage or Dockerfile'
                 'in top level of archive')
            )
        try:
            try:
                tar_ball.getmember('./Procfile.cabotage')
            except KeyError:
                tar_ball.getmember('./Procfile')
        except KeyError:
            raise BuildError(
                'must include a Procfile.cabotage or Procfile '
                'in top level of archive'
            )
        tar_ball.extractall(path=temp_dir, numeric_owner=False)
        if os.path.exists(os.path.join(temp_dir, 'Procfile.cabotage')):
            shutil.copy(
                os.path.join(temp_dir, 'Procfile.cabotage'),
                os.path.join(temp_dir, 'Procfile'),
            )
        with open(os.path.join(temp_dir, 'Procfile'), 'r') as img_procfile:
            procfile_body = img_procfile.read()
        if os.path.exists(os.path.join(temp_dir, 'Dockerfile.cabotage')):
            shutil.copy(
                os.path.join(temp_dir, 'Dockerfile.cabotage'),
                os.path.join(temp_dir, 'Dockerfile'),
            )
        with open(os.path.join(temp_dir, 'Dockerfile'), 'r') as img_dockerfile:
            dockerfile_body = img_dockerfile.read()
            dockerfile_object = DockerfileParser(temp_dir)
            dockerfile_env_vars = list(dockerfile_object.envs.keys())
        image.dockerfile = dockerfile_body
        image.procfile = procfile_body
        db.session.commit()
        try:
            processes = procfile.loads(procfile_body)
        except ValueError as exc:
            raise BuildError(
                f'error parsing Procfile: {exc}'
            )
        tls_config = False
        if docker_secure:
            tls_config = docker.tls.TLSConfig(client_cert=None, ca_cert=docker_ca, verify=True, ssl_version='PROTOCOL_TLSv1_2')
        client = docker.DockerClient(base_url=docker_url, tls=tls_config)
        response = client.api.build(
            path=temp_dir,
            tag=f'{registry}/{image.repository_name}:image-{image.version}',
            rm=True,
            forcerm=True,
            dockerfile="Dockerfile",
            buildargs=image.buildargs(config_writer),
        )
        build_errored = False
        log_lines = []
        for chunk in response:
            for line in chunk.split(b'\r\n'):
                if line:
                    payload = json.loads(line.decode())
                    stream = payload.get('stream')
                    status = payload.get('status')
                    aux = payload.get('aux')
                    error = payload.get('error')
                    if stream:
                        log_lines.append(stream)
                    if status:
                        if payload.get('progressDetail'):
                            continue
                        if payload.get("id"):
                          log_lines.append(f'{payload.get("id")}: {status}\n')
                        else:
                          log_lines.append(f'{status}\n')
                    if error is not None:
                        errorDetail = payload.get('errorDetail', {})
                        message = errorDetail.get('message', 'unknown error')
                        build_errored = (
                            f'Error building image: {message}'
                        )
                    if aux:
                        if 'ID' in aux:
                            built_id = aux['ID']
                            log_lines.append(f'Built Image with ID: {built_id}\n')
        image.image_build_log = ''.join(log_lines)
        db.session.commit()
        if build_errored:
            raise BuildError(build_errored)
        built_image = client.images.get(
            f'{registry}/{image.repository_name}:image-{image.version}'
        )
        client.login(
            username=registry_username,
            password=registry_password,
            registry=registry,
            reauth=True,
        )
        client.images.push(
            f'{registry}/{image.repository_name}',
            f'image-{image.version}'
        )
        pushed_image = client.images.get(
            f'{registry}/{image.repository_name}:image-{image.version}'
        )
        return {
            'image_id': pushed_image.id,
            'processes': processes,
            'dockerfile': dockerfile_body,
            'procfile': procfile_body,
            'dockerfile_env_vars': dockerfile_env_vars,
        }


@shared_task()
def run_image_build(image_id=None):
    secret = current_app.config['REGISTRY_AUTH_SECRET']
    registry = current_app.config['REGISTRY']
    object_bucket = current_app.config['MINIO_BUCKET']
    docker_url = current_app.config['DOCKER_URL']
    docker_secure = current_app.config['DOCKER_SECURE']
    docker_ca = current_app.config['DOCKER_VERIFY']
    image = Image.query.filter_by(id=image_id).first()
    if image is None:
        raise KeyError(f'Image with ID {image_id} not found!')
    credentials = generate_docker_credentials(
        secret=secret,
        resource_type="repository",
        resource_name=image.repository_name,
        resource_actions=["push", "pull"],
    )
    minio_client = minio.minio_connection
    try:
        data = minio_client.get_object(object_bucket, image.build_slug)
        with TemporaryFile() as fp:
            for chunk in data.stream(32*1024):
                fp.write(chunk)
            fp.seek(0)
            with gzip.open(fp, 'rb') as fd:
                try:
                    build_metadata = build_image(
                        fd, image,
                        registry, f'cabotage-builder-{image.id}', credentials,
                        docker_url, docker_secure, docker_ca
                    )
                    image.image_id = build_metadata['image_id']
                    image.processes = build_metadata['processes']
                    image.built = True
                    if image.image_metadata is None:
                        image.image_metadata = {'dockerfile_env_vars': build_metadata['dockerfile_env_vars']}
                    else:
                        image.image_metadata['dockerfile_env_vars'] = build_metadata['dockerfile_env_vars']
                except BuildError as exc:
                    image.error = True
                    image.error_detail = str(exc)
        db.session.commit()
        if image.built and image.image_metadata and image.image_metadata.get('auto_deploy', False):
            if 'installation_id' in image.image_metadata and 'statuses_url' in image.image_metadata:
                access_token = github_app.fetch_installation_access_token(image.image_metadata['installation_id'])
                post_deployment_status_update(
                    access_token, image.image_metadata['statuses_url'],
                    'pending', 'Image built, Release build commencing.'
                )
            release = image.application.create_release()
            release.release_metadata = image.image_metadata
            db.session.add(release)
            db.session.flush()
            activity = Activity(
                verb='create',
                object=release,
                data={
                    'user_id': 'automation',
                    'deployment_id': image.image_metadata.get('id', None),
                    'description': image.image_metadata.get('description', None),
                    'timestamp': datetime.datetime.utcnow().isoformat(),
                }
            )
            db.session.add(activity)
            db.session.commit()
            run_release_build.delay(release_id=release.id)
    except Exception:
        raise


@shared_task()
def run_release_build(release_id=None):
    try:
        secret = current_app.config['REGISTRY_AUTH_SECRET']
        registry = current_app.config['REGISTRY']
        object_bucket = current_app.config['MINIO_BUCKET']
        docker_url = current_app.config['DOCKER_URL']
        docker_secure = current_app.config['DOCKER_SECURE']
        docker_ca = current_app.config['DOCKER_VERIFY']
        release = Release.query.filter_by(id=release_id).first()
        if release is None:
            raise KeyError(f'Release with ID {release_id} not found!')
        credentials = generate_docker_credentials(
            secret=secret,
            resource_type="repository",
            resource_name=release.repository_name,
            resource_actions=["push", "pull"],
        )
        try:
            build_metadata = build_release(
                release,
                registry, f'cabotage-builder-{release.id}', credentials,
                docker_url, docker_secure, docker_ca
            )
            release.release_id = build_metadata['release_id']
            release.built = True
        except BuildError as exc:
            release.error = True
            release.error_detail = str(exc)
        except Exception:
            raise
        db.session.commit()
        if release.built and release.release_metadata and release.release_metadata.get('auto_deploy', False):
            if 'installation_id' in release.release_metadata and 'statuses_url' in release.release_metadata:
                access_token = github_app.fetch_installation_access_token(release.release_metadata['installation_id'])
                post_deployment_status_update(
                    access_token, release.release_metadata['statuses_url'],
                    'pending', 'Release built, Deployment commencing.'
                )
            deployment = Deployment(
                application_id=release.application.id,
                release=release.asdict,
                deploy_metadata=release.release_metadata,
            )
            db.session.add(deployment)
            db.session.flush()
            activity = Activity(
                verb='create',
                object=deployment,
                data={
                    'user_id': 'automation',
                    'deployment_id': release.release_metadata.get('id', None),
                    'description': release.release_metadata.get('description', None),
                    'timestamp': datetime.datetime.utcnow().isoformat(),
                }
            )
            db.session.add(activity)
            db.session.commit()
            if current_app.config['KUBERNETES_ENABLED']:
                deployment_id = deployment.id
                run_deploy.delay(deployment_id=deployment.id)
                deployment = Deployment.query.filter_by(id=deployment_id).first()
            else:
                from cabotage.celery.tasks.deploy import fake_deploy_release
                fake_deploy_release(deployment)
                deployment.complete = True
                db.session.commit()
    except Exception:
        raise
