import datetime
import gzip
import io
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys

from celery import shared_task
from base64 import b64encode, b64decode

import kubernetes
import requests

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
from dxf import DXF

from cabotage.celery.tasks.deploy import run_deploy, run_job

from cabotage.server import (
    db,
    github_app,
    minio,
    config_writer,
    kubernetes as kubernetes_ext,
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
    generate_kubernetes_imagepullsecrets,
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
            fd.write(f'COPY envconsul-linux-amd64 /usr/bin/envconsul-linux-amd64\n')
            fd.write(f'COPY envconsul-linux-arm64 /usr/bin/envconsul-linux-arm64\n')
            fd.write('RUN case $(uname -m) in \\\n')
            fd.write('         "x86_64")  ARCH=amd64 ;; \\\n')
            fd.write('         "aarch64")  ARCH=arm64 ;; \\\n')
            fd.write('    esac \\\n')
            fd.write('&& mv /usr/bin/envconsul-linux-${ARCH} /usr/bin/envconsul\n')
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
        shutil.copy(
            'envconsul-linux-arm64',
            os.path.join(temp_dir, 'envconsul-linux-arm64'),
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

def _fetch_github_file(github_repository="owner/repo", ref="main", access_token=None, filename="Dockerfile"):
    headers = {
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    if access_token is not None:
        headers['Authorization'] = f'token {access_token}'
    response = requests.get(
        f"https://api.github.com/repos/{github_repository}/contents/{filename}",
        params={
            'ref': ref
        },
        headers=headers,
    )
    if response.status_code == 404:
        return None
    if response.status_code == 200:
        data = response.json()
        if data['encoding'] == 'base64':
            return b64decode(response.json()['content']).decode()
    response.raise_for_status()

def build_image_buildkit(image=None):
    secret = current_app.config['REGISTRY_AUTH_SECRET']
    registry = current_app.config['REGISTRY_BUILD']
    registry_secure = current_app.config['REGISTRY_SECURE']
    buildkitd_url = docker_url = current_app.config['BUILDKITD_URL']
    buildkitd_secure = current_app.config['BUILDKITD_SECURE']
    buildkitd_ca = current_app.config['BUILDKITD_VERIFY']

    access_token = None
    # TODO: Do the GitHub Dance we'll want to auth if we ever do private repoz
    #bearer_token = github_app.bearer_token
    #access_token_response = requests.post(
    #    f'https://api.github.com/app/installations/{installation_id}/access_tokens',
    #    headers={
    #        'Accept': 'application/vnd.github.machine-man-preview+json',
    #        'Authorization': f'Bearer {bearer_token}',
    #    }
    #)
    #if 'token' not in access_token_response.json():
    #    print(f'Unable to authenticate for {installation_id}')
    #    print(access_token_response.json())
    #    raise BuildError(f'Unable to authenticate for {installation_id}')
    #access_token = access_token_response.json()

    dockerfile_body = _fetch_github_file(image.application.github_repository, image.build_ref, access_token=access_token, filename='Dockerfile.cabotage')
    if dockerfile_body is None:
        dockerfile_body = _fetch_github_file(image.application.github_repository, image.build_ref, access_token=access_token, filename='Dockerfile')
    if dockerfile_body is None:
       raise BuildError(f'No Dockerfile.cabotage or Dockerfile found in root of {image.application.github_repository}@{image.build_ref}')

    procfile_body = _fetch_github_file(image.application.github_repository, image.build_ref, access_token=access_token, filename='Procfile.cabotage')
    if procfile_body is None:
        procfile_body = _fetch_github_file(image.application.github_repository, image.build_ref, access_token=access_token, filename='Procfile')
    if procfile_body is None:
       raise BuildError(f'No Procfile.cabotage or Procfile found in root of {image.application.github_repository}@{image.build_ref}')

    image.dockerfile = dockerfile_body
    image.procfile = procfile_body
    db.session.commit()

    dockerfile_object = DockerfileParser()
    with TemporaryDirectory() as tempdir:
        previous_dir = os.getcwd()
        os.chdir(tempdir)
        try:
            dockerfile_object.content = dockerfile_body
        finally:
            os.chdir(previous_dir)
    dockerfile_env_vars = list(dockerfile_object.envs.keys())
    try:
        processes = procfile.loads(procfile_body)
    except ValueError as exc:
        raise BuildError(
            f'error parsing Procfile: {exc}'
        )

    insecure_reg=""
    registry_url=f"https://{registry}/v2"
    if not registry_secure:
        insecure_reg=",registry.insecure=true"
        registry_url=f"http://{registry}/v2"

    dockerconfigjson = generate_kubernetes_imagepullsecrets(
        secret=secret,
        registry_urls=[registry_url],
        resource_type='repository',
        resource_name=image.repository_name,
        resource_actions=['push', 'pull'],
    )

    buildctl_command = [
        "/usr/bin/buildctl",
    ]
    buildctl_args = [
        "build",
        "--progress=plain",
        "--frontend",
        "dockerfile.v0",
        "--opt",
        f"context=https://github.com/{image.application.github_repository}.git#{image.build_ref}",
        "--import-cache",
        f"type=registry,ref={registry}/{image.repository_name}:buildcache{insecure_reg}",
        "--export-cache",
        f"type=registry,ref={registry}/{image.repository_name}:buildcache{insecure_reg}",
        "--output",
        f"type=image,name={registry}/{image.repository_name}:image-{image.version},push=true{insecure_reg}",
    ]


    if current_app.config['KUBERNETES_ENABLED']:
        job_id = secrets.token_hex(4)
        secret_object = kubernetes.client.V1Secret(
            type='kubernetes.io/dockerconfigjson',
            metadata=kubernetes.client.V1ObjectMeta(
                name=f'buildkit-registry-auth-{job_id}',
            ),
            data={
                '.dockerconfigjson': b64encode(dockerconfigjson.encode()).decode(),
            }
        )
        job_object = kubernetes.client.V1Job(
            metadata=kubernetes.client.V1ObjectMeta(
                name=f'imagebuild-{job_id}',
                labels={
                    'organization': image.application.project.organization.slug,
                    'project': image.application.project.slug,
                    'application': image.application.slug,
                }
            ),
            spec=kubernetes.client.V1JobSpec(
                active_deadline_seconds=1800,
                backoff_limit=0,
                parallelism=1,
                completions=1,
                template=kubernetes.client.V1PodTemplateSpec(
                    metadata=kubernetes.client.V1ObjectMeta(
                        labels={
                            'organization': image.application.project.organization.slug,
                            'project': image.application.project.slug,
                            'application': image.application.slug,
                            'process': 'build',
                        },
                    ),
                    spec=kubernetes.client.V1PodSpec(
                        restart_policy="Never",
                        containers=[
                            kubernetes.client.V1Container(
                                name="build",
                                image="moby/buildkit:v0.11.3-rootless",
                                command=buildctl_command,
                                args=buildctl_args,
                                env=[
                                    kubernetes.client.V1EnvVar(name="BUILDKIT_HOST", value=buildkitd_url),
                                ],
                                security_context=kubernetes.client.V1SecurityContext(
                                    allow_privilege_escalation=False,
                                    run_as_user=1000,
                                    run_as_group=1000,
                                ),
                                volume_mounts=[
                                    kubernetes.client.V1VolumeMount(
                                        mount_path="/home/user/.local/share/buildkit",
                                        name="buildkitd",
                                    ),
                                    kubernetes.client.V1VolumeMount(
                                        mount_path="/home/user/.docker",
                                        name="buildkit-registry-auth",
                                    ),
                                ]
                            ),
                        ],
                        volumes=[
                            kubernetes.client.V1Volume(
                                name="buildkitd",
                                empty_dir=kubernetes.client.V1EmptyDirVolumeSource(),
                            ),
                            kubernetes.client.V1Volume(
                                name="buildkit-registry-auth",
                                secret=kubernetes.client.V1SecretVolumeSource(
                                    secret_name=f"buildkit-registry-auth-{job_id}",
                                    items=[
                                        kubernetes.client.V1KeyToPath(
                                            key='.dockerconfigjson',
                                            path='config.json',
                                        ),
                                    ]
                                )
                            ),
                        ]
                    ),
                ),
            ),
        )

        api_client = kubernetes_ext.kubernetes_client
        core_api_instance = kubernetes.client.CoreV1Api(api_client)
        batch_api_instance = kubernetes.client.BatchV1Api(api_client)
        core_api_instance.create_namespaced_secret('default', secret_object)

        job_complete, job_logs = run_job(core_api_instance, batch_api_instance, 'default', job_object)

        image.image_build_log = job_logs
        db.session.commit()
        if not job_complete:
            raise BuildError(f'Image build failed!')
        db.session.commit()
    else:
        with TemporaryDirectory() as tempdir:
            os.makedirs(os.path.join(tempdir, '.docker'), exist_ok=True)
            with open(os.path.join(tempdir, '.docker', 'config.json'), 'w') as f:
                f.write(dockerconfigjson)
            completed_subprocess = subprocess.run(
                " ".join(buildctl_command + buildctl_args),
                env={'BUILDKIT_HOST': buildkitd_url, 'HOME': tempdir},
                shell=True, cwd="/tmp",
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
        image.image_build_log = " ".join(buildctl_command + buildctl_args) + "\n" + completed_subprocess.stdout
        db.session.commit()

    def auth(dxf, response):
        dxf.token = generate_docker_registry_jwt(access=[{"type": "repository", "name": image.repository_name, "actions": ["pull"]}])

    _tlsverify = False
    if registry_secure:
        _tlsverify = current_app.config['REGISTRY_VERIFY']
        if _tlsverify == 'True':
            _tlsverify = True
    client = DXF(
        host=registry,
        repo=image.repository_name,
        auth=auth,
        insecure=(not registry_secure),
        tlsverify=_tlsverify,
    )
    pushed_image = client.get_digest(
        f'image-{image.version}'
    )
    return {
        'image_id': pushed_image,
        'processes': processes,
        'dockerfile': dockerfile_body,
        'procfile': procfile_body,
        'dockerfile_env_vars': dockerfile_env_vars,
    }


@shared_task()
def run_image_build(image_id=None, buildkit=False):
    secret = current_app.config['REGISTRY_AUTH_SECRET']
    registry = current_app.config['REGISTRY_BUILD']
    object_bucket = current_app.config['MINIO_BUCKET']
    docker_url = current_app.config['DOCKER_URL']
    docker_secure = current_app.config['DOCKER_SECURE']
    docker_ca = current_app.config['DOCKER_VERIFY']
    image = Image.query.filter_by(id=image_id).first()
    if image is None:
        raise KeyError(f'Image with ID {image_id} not found!')

    if buildkit:
        try:
            try:
                build_metadata = build_image_buildkit(image)
            except BuildError as exc:
                image.error = True
                image.error_detail = str(exc)
            db.session.commit()
        except Exception:
            raise
    else:
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
                    except BuildError as exc:
                        image.error = True
                        image.error_detail = str(exc)
            db.session.commit()
        except Exception:
            raise

    image.image_id = build_metadata['image_id']
    image.processes = build_metadata['processes']
    image.built = True
    if image.image_metadata is None:
        image.image_metadata = {'dockerfile_env_vars': build_metadata['dockerfile_env_vars']}
    else:
        image.image_metadata['dockerfile_env_vars'] = build_metadata['dockerfile_env_vars']

    db.session.add(image)
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

@shared_task()
def run_release_build(release_id=None):
    try:
        secret = current_app.config['REGISTRY_AUTH_SECRET']
        registry = current_app.config['REGISTRY_BUILD']
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
