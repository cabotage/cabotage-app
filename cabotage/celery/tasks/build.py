import gzip
import io
import json
import os
import shutil
import stat
import sys

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
import procfile

from flask import current_app

from cabotage.server import (
    db,
    celery,
    minio,
)

from cabotage.server.models.projects import (
    Image,
    Release,
)

from cabotage.utils.docker_auth import (
    check_docker_credentials,
    generate_docker_credentials,
    generate_docker_registry_jwt,
    parse_docker_scope,
    docker_access_intersection,
)


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
                  docker_url, docker_secure):
    with ExitStack() as stack:
        temp_dir = stack.enter_context(TemporaryDirectory())
        with open(os.path.join(temp_dir, 'entrypoint.sh'), 'w') as fd:
            fd.write(ENTRYPOINT)
        st = os.stat(os.path.join(temp_dir, 'entrypoint.sh'))
        os.chmod(os.path.join(temp_dir, 'entrypoint.sh'), st.st_mode | stat.S_IEXEC)
        with open(os.path.join(temp_dir, 'Dockerfile'), 'a') as fd:
            fd.write(RELEASE_DOCKERFILE_TEMPLATE.format(registry=registry, image=release.image_object))
            fd.write(f'COPY envconsul-linux-amd64 /usr/bin/envconsul\n')
            fd.write(f'COPY entrypoint.sh /entrypoint.sh\n')
            for process_name in  release.envconsul_configurations:
                fd.write(f'COPY envconsul-{process_name}.hcl /etc/cabotage/envconsul-{process_name}.hcl\n')
            fd.write(f'ENTRYPOINT ["/entrypoint.sh"]\n')
            fd.write(f'CMD []\n')
        with open(os.path.join(temp_dir, 'Dockerfile'), 'rU') as release_dockerfile:
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
        client = docker.DockerClient(base_url=docker_url, tls=docker_secure)
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
                docker_url, docker_secure):
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
                pass
            tar_ball.getmember('./Dockerfile')
        except KeyError:
            raise BuildError(
                ('must include a Dockerfile or Dockerfile.cabotage '
                 'in top level of archive')
            )
        try:
            tar_ball.getmember('./Procfile')
        except KeyError:
            raise BuildError(
                'must include a Procfile in top level of archive'
            )
        tar_ball.extractall(path=temp_dir, numeric_owner=False)
        with open(os.path.join(temp_dir, 'Procfile'), 'rU') as img_procfile:
            procfile_body = img_procfile.read()
        with open(os.path.join(temp_dir, 'Dockerfile'), 'rU') as img_dockerfile:
            dockerfile_body = img_dockerfile.read()
        image.dockerfile = dockerfile_body
        image.procfile = procfile_body
        db.session.commit()
        try:
            processes = procfile.loads(procfile_body)
        except ValueError as exc:
            raise BuildError(
                f'error parsing Procfile: {exc}'
            )
        client = docker.DockerClient(base_url=docker_url, tls=docker_secure)
        response = client.api.build(
            path=temp_dir,
            tag=f'{registry}/{image.repository_name}:image-{image.version}',
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
        }


@celery.task()
def run_image_build(image_id=None):
    secret = current_app.config['CABOTAGE_REGISTRY_AUTH_SECRET']
    registry = current_app.config['CABOTAGE_REGISTRY']
    object_bucket = current_app.config['CABOTAGE_MINIO_BUCKET']
    docker_url = current_app.config['CABOTAGE_DOCKER_URL']
    docker_secure = current_app.config['CABOTAGE_DOCKER_SECURE']
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
                        docker_url, docker_secure
                    )
                    image.image_id = build_metadata['image_id']
                    image.processes = build_metadata['processes']
                    image.built = True
                except BuildError as exc:
                    image.error = True
                    image.error_detail = str(exc)
        db.session.commit()
    except Exception:
        raise


@celery.task()
def run_release_build(release_id=None):
    secret = current_app.config['CABOTAGE_REGISTRY_AUTH_SECRET']
    registry = current_app.config['CABOTAGE_REGISTRY']
    object_bucket = current_app.config['CABOTAGE_MINIO_BUCKET']
    docker_url = current_app.config['CABOTAGE_DOCKER_URL']
    docker_secure = current_app.config['CABOTAGE_DOCKER_SECURE']
    release = Release.query.filter_by(id=release_id).first()
    if release is None:
        raise KeyError(f'Release with ID {release_id} not found!')
    credentials = generate_docker_credentials(
        secret=secret,
        resource_type="repository",
        resource_name=release.repository_name,
        resource_actions=["push", "pull"],
    )
    print(
        release,
        registry, f'cabotage-builder-{release.id}', credentials,
        docker_url, docker_secure
    )
    try:
        build_metadata = build_release(
            release,
            registry, f'cabotage-builder-{release.id}', credentials,
            docker_url, docker_secure
        )
        release.release_id = build_metadata['release_id']
        release.built = True
    except BuildError as exc:
        release.error = True
        release.error_detail = str(exc)
    except Exception:
        raise
    db.session.commit()
