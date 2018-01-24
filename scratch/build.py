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
import minio


def build_image(tarfileobj, registry, org_slug, project_slug, application_slug, version):
    with ExitStack() as stack:
        temp_dir = stack.enter_context(TemporaryDirectory())
        tar_ball = stack.enter_context(TarFile(fileobj=tarfileobj, mode='r'))
        for tarinfo in tar_ball:
            if os.path.normpath(tarinfo.name).startswith((os.sep, '/', '..')):
                raise RuntimeError(
                    ('refusing to touch sketchy tarball, '
                     'no relative paths outside of root directory allowed '
                     f'{tarinfo.name} exits top level directory')
                )
            if not (tarinfo.isfile() or tarinfo.isdir()):
                raise RuntimeError(
                    ('refusing to touch sketchy tarball, '
                     'only regular files and directories allowed '
                     f'{tarinfo.name} is not a regular file or directory')
                )
        try:
            tar_ball.getmember('./Dockerfile')
        except KeyError:
            raise RuntimeError(
                ('must include a Dockerfile or Dockerfile.cabotage '
                 'in top level of archive')
            )
        try:
            tar_ball.getmember('./Procfile')
        except KeyError:
            raise RuntimeError(
                'must include a Procfile in top level of archive'
            )
        tar_ball.extractall(path=temp_dir, numeric_owner=False)
        shutil.copy(
            'envconsul-linux-amd64',
            os.path.join(temp_dir, 'envconsul-linux-amd64'),
        )
        with open(os.path.join(temp_dir, 'Dockerfile'), 'a') as fd:
            fd.write(f'COPY envconsul-linux-amd64 /usr/bin/envconsul\n')
        client = docker.DockerClient(base_url='tcp://127.0.0.1:2375', tls=False)
        tag = f'cabotage/{org_slug}_{project_slug}_{application_slug}'
        response = client.api.build(
            path=temp_dir,
            tag=f'{registry}/{tag}:{version}',
            rm=True,
            forcerm=True,
            dockerfile="Dockerfile",
        )
        for chunk in response:
            print(chunk)
            for line in chunk.split(b'\r\n'):
                if line:
                    payload = json.loads(line.decode())
                    aux = payload.get('aux')
                    stream = payload.get('stream')
                    status = payload.get('status')
                    if stream:
                        sys.stderr.write(stream)
        image = client.images.get(f'{registry}/{tag}:{version}')
        print(image.id)
        print(client.images.push(f'{registry}/{tag}', f'{version}'))


if __name__ == '__main__':
    import click
    @click.command()
    @click.option('--object-bucket', default='cabotage-builds')
    @click.option('--object-path', default='org/project/app/deadbeef.tar.gz')
    @click.option('--minio-endpoint', default='127.0.0.1:9000')
    @click.option('--minio-access-key', default='MINIOACCESSKEY')
    @click.option('--minio-secret-key', default='MINIOSECRETKEY')
    @click.option('--minio-secure', default=False)
    @click.option('--registry', default='registry:5000')
    @click.option('--registry-token', default=None)
    @click.option('--organization-slug', default='org')
    @click.option('--project-slug', default='proj')
    @click.option('--application-slug', default='app')
    @click.option('--version', default=1)
    def run_build(object_bucket, object_path,
                  minio_endpoint, minio_access_key, minio_secret_key, minio_secure,
                  registry, registry_token,
                  organization_slug, project_slug, application_slug, version):
        minio_client = minio.Minio(minio_endpoint, access_key=minio_access_key, secret_key=minio_secret_key, secure=minio_secure)
        try:
            data = minio_client.get_object(object_bucket, object_path)
            with TemporaryFile() as fp:
                for chunk in data.stream(32*1024):
                    fp.write(chunk)
                fp.seek(0)
                with gzip.open(fp, 'rb') as fd:
                    build_image(fd, registry, organization_slug, project_slug, application_slug, version)
            minio_client.remove_object(object_bucket, object_path)
        except Exception:
            raise

    run_build()
