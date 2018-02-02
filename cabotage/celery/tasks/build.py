import gzip
import io
import json
import os
import shutil
import sys

from contextlib import ExitStack
from tarfile import TarFile
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

from cabotage.server.models.projects import Image

from cabotage.utils.docker_auth import (
    check_docker_credentials,
    generate_docker_credentials,
    generate_docker_registry_jwt,
    parse_docker_scope,
    docker_access_intersection,
)


class BuildError(RuntimeError):
    pass


def build_image(tarfileobj, image,
                registry, registry_username, registry_password,
                docker_url, docker_secure):
    with ExitStack() as stack:
        temp_dir = stack.enter_context(TemporaryDirectory())
        tar_ball = stack.enter_context(TarFile(fileobj=tarfileobj, mode='r'))
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
        with open(os.path.join(temp_dir, 'Dockerfile'), 'a') as fd:
            fd.write(f'COPY envconsul-linux-amd64 /usr/bin/envconsul\n')
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
        shutil.copy(
            'envconsul-linux-amd64',
            os.path.join(temp_dir, 'envconsul-linux-amd64'),
        )
        client = docker.DockerClient(base_url=docker_url, tls=docker_secure)
        response = client.api.build(
            path=temp_dir,
            tag=f'{registry}/{image.repository_name}:{image.version}',
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
                    sys.stderr.write(json.dumps(payload))
                    stream = payload.get('stream')
                    status = payload.get('status')
                    if status:
                        if payload.get('progressDetail'):
                            continue
                    aux = payload.get('aux')
                    error = payload.get('error')
                    if error is not None:
                        errorDetail = payload.get('errorDetail', {})
                        message = errorDetail.get('message', 'unknown error')
                        build_errored = (
                            f'Error building image: {message}'
                        )
                    log_lines.append(json.dumps(payload))
        if build_errored:
            raise BuildError(build_errored)
        image.image_build_log = '\n'.join(log_lines)
        db.session.commit()
        built_image = client.images.get(
            f'{registry}/{image.repository_name}:{image.version}'
        )
        client.login(
            username=registry_username,
            password=registry_password,
            registry=registry,
        )
        client.images.push(
            f'{registry}/{image.repository_name}',
            f'{image.version}'
        )
        pushed_image = client.images.get(
            f'{registry}/{image.repository_name}:{image.version}'
        )
        return {
            'image_id': pushed_image.id,
            'processes': processes,
            'dockerfile': dockerfile_body,
            'procfile': procfile_body,
        }


@celery.task()
def run_build(image_id=None):
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
                        registry, 'cabotage-builder', credentials,
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
