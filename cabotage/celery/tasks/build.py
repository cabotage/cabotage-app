import datetime
import os
import re
import secrets
import shlex
import subprocess  # nosec

from celery import shared_task
from base64 import b64encode, b64decode

import kubernetes
import toml

from kubernetes.client.rest import ApiException

from tempfile import (
    TemporaryDirectory,
)

from dockerfile_parse import DockerfileParser
from flask import current_app
from dxf import DXF
from github import Github
from github.Auth import AppAuth as GithubAppAuth
from github.GithubException import GithubException, UnknownObjectException
from github.GithubIntegration import GithubIntegration

from cabotage.celery.tasks.deploy import run_deploy, run_job

from cabotage.server import (
    db,
    github_app,
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
    generate_docker_registry_jwt,
    generate_kubernetes_imagepullsecrets,
)

from cabotage.utils.build_log_stream import (
    get_redis_client,
    publish_end,
    refresh_heartbeat,
    run_and_stream,
    stream_key,
)
from cabotage.utils.release_build_context import RELEASE_DOCKERFILE_TEMPLATE
from cabotage.utils.github import (
    CheckRun,
    cabotage_url,
    post_deployment_status_update,
)
from cabotage.utils import procfile

Activity = activity_plugin.activity_cls


class BuildError(RuntimeError):
    pass


class BuildkitEnv:
    """Shared registry and buildkit configuration."""

    def __init__(self, repository_name):
        self.secret = current_app.config["REGISTRY_AUTH_SECRET"]
        self.registry = current_app.config["REGISTRY_BUILD"]
        self.registry_secure = current_app.config["REGISTRY_SECURE"]
        self.registry_ca = current_app.config["REGISTRY_VERIFY"]
        self.buildkit_image = current_app.config["BUILDKIT_IMAGE"]

        self.insecure_reg = ""
        registry_url = f"https://{self.registry}/v2"
        if not self.registry_secure:
            self.insecure_reg = ",registry.insecure=true"
            registry_url = f"http://{self.registry}/v2"

        self.dockerconfigjson = generate_kubernetes_imagepullsecrets(
            secret=self.secret,
            registry_urls=[registry_url],
            resource_type="repository",
            resource_name=repository_name,
            resource_actions=["push", "pull"],
        )
        buildkitd_config = {
            "registry": {
                self.registry: {
                    "insecure": not self.registry_secure,
                    "ca": [
                        ca
                        for ca in [self.registry_ca]
                        if self.registry_secure and not isinstance(ca, bool)
                    ],
                }
            },
        }
        if not self.registry_secure:
            buildkitd_config["registry"][self.registry]["http"] = True
        self.buildkitd_toml = toml.dumps(buildkitd_config)

    def verify_registry_tag(self, repository_name, tag):
        """Verify a tag was pushed to the registry. Returns the digest."""

        def auth(dxf, response):
            dxf.token = generate_docker_registry_jwt(
                access=[
                    {
                        "type": "repository",
                        "name": repository_name,
                        "actions": ["pull"],
                    }
                ]
            )

        _tlsverify = False
        if self.registry_secure:
            _tlsverify = self.registry_ca
            if _tlsverify == "True":
                _tlsverify = True
        client = DXF(
            host=self.registry,
            repo=repository_name,
            auth=auth,
            insecure=(not self.registry_secure),
            tlsverify=_tlsverify,
        )
        return client.get_digest(tag)

    def tls_context_args(self):
        """Return --registry-auth-tlscontext args if needed, else empty list."""
        if self.registry_ca and not isinstance(self.registry_ca, bool):
            return [
                "--registry-auth-tlscontext",
                f"host={self.registry},ca={self.registry_ca}",
            ]
        return []


def _fetch_github_access_token(application):
    """Get a GitHub access token for the application's installation."""
    access_token = current_app.config.get("GITHUB_TOKEN")
    if (
        application.github_repository_is_private
        or application.github_app_installation_id
    ):
        try:
            auth = GithubAppAuth(github_app.app_id, github_app.app_private_key_pem)
            gi = GithubIntegration(auth=auth)
            access_token = gi.get_access_token(
                application.github_app_installation_id
            ).token
            if access_token is None:
                raise Exception
        except Exception:
            raise BuildError(
                "Unable to authenticate for Installation ID "
                f"{application.github_app_installation_id}"
            )
    return access_token


def _fetch_image_source(image, access_token):
    """Fetch Dockerfile, Procfile from GitHub and parse processes.

    Returns dict with keys: dockerfile_body, dockerfile_name, procfile_body,
    processes, dockerfile_env_vars.  Also updates image.dockerfile/procfile
    and commits.
    """
    if image.commit_sha == "null":
        commit_sha = _fetch_commit_sha_for_ref(
            image.application.github_repository,
            image.build_ref,
            access_token=access_token,
        )
        # Reassign the whole dict so SQLAlchemy detects the JSONB mutation
        image.image_metadata = {**(image.image_metadata or {}), "sha": commit_sha}

    def git_ref(repository, sha):
        git_sha = image.commit_sha
        if git_sha == "null":
            git_sha = image.build_ref or "main"
        ref = f"https://x-access-token@github.com/{image.application.github_repository}.git#{git_sha}"
        if image.application.subdirectory:
            return f"{ref}:{image.application.subdirectory}"
        return ref

    def file_path(filename):
        if image.application.subdirectory:
            return os.path.join(image.application.subdirectory, filename)
        return filename

    dockerfile_candidates = ["Dockerfile.cabotage", "Dockerfile"]
    if image.application.dockerfile_path:
        dockerfile_candidates = [image.application.dockerfile_path]

    dockerfile_name = None
    dockerfile_body = None
    for candidate in dockerfile_candidates:
        dockerfile_body = _fetch_github_file(
            image.application.github_repository,
            image.commit_sha,
            access_token=access_token,
            filename=file_path(candidate),
        )
        if dockerfile_body is not None:
            dockerfile_name = candidate
            break
    if dockerfile_body is None:
        raise BuildError(
            f"No Dockerfile found in "
            f"{git_ref(image.application.github_repository, image.commit_sha)}"
        )

    procfile_body = _fetch_github_file(
        image.application.github_repository,
        image.commit_sha,
        access_token=access_token,
        filename=file_path("Procfile.cabotage"),
    )
    if procfile_body is None:
        procfile_body = _fetch_github_file(
            image.application.github_repository,
            image.commit_sha,
            access_token=access_token,
            filename=file_path("Procfile"),
        )
    if procfile_body is None:
        raise BuildError(
            "No Procfile.cabotage or Procfile found in root of "
            f"{git_ref(image.application.github_repository, image.commit_sha)}"
        )

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
        raise BuildError(f"error parsing Procfile: {exc}")

    for process_name in processes.keys():
        if re.search("\s", process_name) is not None:
            raise BuildError(
                f'Invalid process name: "{process_name}" in Procfile, '
                "may not contain whitespace."
            )

    return {
        "git_ref": git_ref,
        "dockerfile_body": dockerfile_body,
        "dockerfile_name": dockerfile_name,
        "procfile_body": procfile_body,
        "processes": processes,
        "dockerfile_env_vars": dockerfile_env_vars,
    }


def build_release_buildkit(release):
    bke = BuildkitEnv(release.repository_name)
    registry = bke.registry
    buildkit_image = bke.buildkit_image

    process_commands = "\n".join(
        [
            (
                f"COPY envconsul-{process_name}.hcl "
                f"/etc/cabotage/envconsul-{process_name}.hcl"
            )
            for process_name in release.envconsul_configurations
        ]
    )
    release.dockerfile = RELEASE_DOCKERFILE_TEMPLATE.format(
        registry=registry,
        image=release.image_snapshot,
        process_commands=process_commands,
    )
    db.session.add(release)
    db.session.commit()

    insecure_reg = bke.insecure_reg
    dockerconfigjson = bke.dockerconfigjson
    buildkitd_toml = bke.buildkitd_toml

    buildctl_command = [
        "buildctl-daemonless.sh",
    ]
    buildctl_args = [
        "build",
        "--progress=plain",
        "--frontend",
        "dockerfile.v0",
        "--output",
        (
            f"type=image,name={registry}/{release.repository_name}"
            f":release-{release.version},push=true{insecure_reg}"
        ),
    ]

    buildctl_args += bke.tls_context_args()

    db.session.add(release)
    try:
        if current_app.config["KUBERNETES_ENABLED"]:
            buildctl_args += [
                "--local",
                "dockerfile=/context",
                "--local",
                "context=/context",
            ]
            api_client = kubernetes_ext.kubernetes_client
            core_api_instance = kubernetes.client.CoreV1Api(api_client)
            batch_api_instance = kubernetes.client.BatchV1Api(api_client)
            # Create PersistentVolumeClaim
            volume_claim = fetch_image_build_cache_volume_claim(
                core_api_instance, release
            )
            docker_secret_object = kubernetes.client.V1Secret(
                type="kubernetes.io/dockerconfigjson",
                metadata=kubernetes.client.V1ObjectMeta(
                    name=f"buildkit-registry-auth-{release.build_job_id}",
                ),
                data={
                    ".dockerconfigjson": b64encode(dockerconfigjson.encode()).decode(),
                },
            )
            buildkitd_toml_configmap_object = kubernetes.client.V1ConfigMap(
                metadata=kubernetes.client.V1ObjectMeta(
                    name=f"buildkitd-toml-{release.build_job_id}",
                ),
                data={
                    "buildkitd.toml": buildkitd_toml,
                },
            )
            context_configmap_object = release.release_build_context_configmap
            job_object = kubernetes.client.V1Job(
                metadata=kubernetes.client.V1ObjectMeta(
                    name=f"releasebuild-{release.build_job_id}",
                    labels={
                        "organization": release.application.project.organization.slug,
                        "project": release.application.project.slug,
                        "application": release.application.slug,
                        "process": "build",
                        "build_id": release.build_job_id,
                        "resident-job.cabotage.io": "true",
                    },
                ),
                spec=kubernetes.client.V1JobSpec(
                    active_deadline_seconds=1800,
                    backoff_limit=0,
                    parallelism=1,
                    completions=1,
                    template=kubernetes.client.V1PodTemplateSpec(
                        metadata=kubernetes.client.V1ObjectMeta(
                            labels={
                                "organization": release.application.project.organization.slug,  # noqa: E501
                                "project": release.application.project.slug,
                                "application": release.application.slug,
                                "process": "build",
                                "build_id": release.build_job_id,
                                "ca-admission.cabotage.io": "true",
                                "resident-pod.cabotage.io": "true",
                            },
                            annotations={
                                "container.apparmor.security.beta.kubernetes.io/build": "unconfined",  # noqa: E501
                            },
                        ),
                        spec=kubernetes.client.V1PodSpec(
                            restart_policy="Never",
                            termination_grace_period_seconds=0,
                            security_context=kubernetes.client.V1PodSecurityContext(
                                fs_group=1000,
                                fs_group_change_policy="OnRootMismatch",
                            ),
                            containers=[
                                kubernetes.client.V1Container(
                                    name="build",
                                    image=buildkit_image,
                                    command=buildctl_command,
                                    args=buildctl_args,
                                    env=[
                                        kubernetes.client.V1EnvVar(
                                            name="BUILDKITD_FLAGS",
                                            value="--config /home/user/.config/buildkit/buildkitd.toml --oci-worker-no-process-sandbox",  # noqa: E501
                                        ),
                                    ],
                                    security_context=kubernetes.client.V1SecurityContext(
                                        seccomp_profile=kubernetes.client.V1SeccompProfile(
                                            type="Unconfined",
                                        ),
                                        run_as_user=1000,
                                        run_as_group=1000,
                                    ),
                                    volume_mounts=[
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/home/user/.local/share/buildkit",
                                            name="build-cache",
                                        ),
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/home/user/.config/buildkit",
                                            name="buildkitd-toml",
                                        ),
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/home/user/.docker",
                                            name="buildkit-registry-auth",
                                        ),
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/context/Dockerfile",
                                            sub_path="Dockerfile",
                                            name="build-context",
                                        ),
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/context/entrypoint.sh",
                                            sub_path="entrypoint.sh",
                                            name="build-context",
                                        ),
                                        *[
                                            kubernetes.client.V1VolumeMount(
                                                mount_path=f"/context/envconsul-{process_name}.hcl",
                                                sub_path=f"envconsul-{process_name}.hcl",
                                                name="build-context",
                                            )
                                            for process_name in release.envconsul_configurations  # noqa: E501
                                        ],
                                    ],
                                ),
                            ],
                            volumes=[
                                kubernetes.client.V1Volume(
                                    name="build-cache",
                                    persistent_volume_claim=kubernetes.client.V1PersistentVolumeClaimVolumeSource(
                                        claim_name=volume_claim.metadata.name
                                    ),
                                ),
                                kubernetes.client.V1Volume(
                                    name="buildkitd-toml",
                                    config_map=kubernetes.client.V1ConfigMapVolumeSource(
                                        name=f"buildkitd-toml-{release.build_job_id}",
                                        items=[
                                            kubernetes.client.V1KeyToPath(
                                                key="buildkitd.toml",
                                                path="buildkitd.toml",
                                            ),
                                        ],
                                    ),
                                ),
                                kubernetes.client.V1Volume(
                                    name="buildkit-registry-auth",
                                    secret=kubernetes.client.V1SecretVolumeSource(
                                        secret_name=f"buildkit-registry-auth-{release.build_job_id}",
                                        items=[
                                            kubernetes.client.V1KeyToPath(
                                                key=".dockerconfigjson",
                                                path="config.json",
                                            ),
                                        ],
                                    ),
                                ),
                                kubernetes.client.V1Volume(
                                    name="build-context",
                                    config_map=kubernetes.client.V1ConfigMapVolumeSource(
                                        name=f"build-context-{release.build_job_id}"
                                    ),
                                ),
                            ],
                        ),
                    ),
                ),
            )

            core_api_instance.create_namespaced_config_map(
                "default", context_configmap_object
            )
            core_api_instance.create_namespaced_config_map(
                "default", buildkitd_toml_configmap_object
            )
            core_api_instance.create_namespaced_secret("default", docker_secret_object)

            try:
                redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
                log_key = stream_key("release", release.build_job_id)
            except Exception:  # nosec B110
                redis_client = None
                log_key = None

            try:
                job_complete, job_logs = run_job(
                    core_api_instance,
                    batch_api_instance,
                    "default",
                    job_object,
                    redis_client=redis_client,
                    log_key=log_key,
                    heartbeat_type="release_build",
                    heartbeat_id=str(release.id),
                )
                if redis_client and log_key:
                    try:
                        publish_end(redis_client, log_key, error=not job_complete)
                    except Exception:  # nosec B110
                        pass
            finally:
                core_api_instance.delete_namespaced_secret(
                    f"buildkit-registry-auth-{release.build_job_id}",
                    "default",
                    propagation_policy="Foreground",
                )
                core_api_instance.delete_namespaced_config_map(
                    f"buildkitd-toml-{release.build_job_id}",
                    "default",
                    propagation_policy="Foreground",
                )
                core_api_instance.delete_namespaced_config_map(
                    f"build-context-{release.build_job_id}",
                    "default",
                    propagation_policy="Foreground",
                )

            db.session.refresh(release)
            release.release_build_log = job_logs
            db.session.commit()
            if not job_complete:
                raise BuildError("Image build failed!")
        else:
            buildctl_args += [
                "--local",
                "dockerfile=context",
                "--local",
                "context=context",
            ]
            context_configmap_object = release.release_build_context_configmap
            with TemporaryDirectory() as tempdir:
                os.makedirs(os.path.join(tempdir, "context"), exist_ok=True)
                for file, contents in context_configmap_object.data.items():
                    with open(os.path.join(tempdir, "context", file), "w") as f:
                        f.write(contents)
                os.makedirs(os.path.join(tempdir, ".docker"), exist_ok=True)
                with open(os.path.join(tempdir, ".docker", "config.json"), "w") as f:
                    f.write(dockerconfigjson)
                with open(os.path.join(tempdir, "buildkitd.toml"), "w") as f:
                    f.write(buildkitd_toml)

                buildkit_root = f"/tmp/buildkit-{release.application.id}-{release.application_environment_id or 'base'}"  # nosec B108 — deterministic path scoped by app+env ID
                os.makedirs(buildkit_root, exist_ok=True)
                sock_addr = f"unix://{buildkit_root}/buildkitd.sock"
                wrapper = os.path.join(tempdir, "buildctl-daemonless.sh")
                with open(wrapper, "w") as f:
                    f.write(
                        "#!/bin/sh\n"
                        "set -eu\n"
                        f"buildkitd --addr={sock_addr} $BUILDKITD_FLAGS &\n"
                        "pid=$!\n"
                        'trap "kill $pid || true; wait $pid || true" EXIT\n'
                        "try=0; max=10\n"
                        f"until buildctl --addr={sock_addr} debug workers >/dev/null 2>&1; do\n"
                        "  if [ $try -gt $max ]; then\n"
                        f'    echo >&2 "could not connect to {sock_addr} after $max trials"\n'
                        "    exit 1\n"
                        "  fi\n"
                        "  sleep 0.1\n"
                        "  try=$((try + 1))\n"
                        "done\n"
                        f'buildctl --addr={sock_addr} "$@"\n'
                    )
                os.chmod(
                    wrapper, 0o755
                )  # nosec B103 — wrapper script must be executable
                buildctl_command = [wrapper]

                try:
                    output = run_and_stream(
                        buildctl_command + buildctl_args,
                        env={
                            **os.environ,
                            "BUILDKITD_FLAGS": (
                                f"--root={buildkit_root}"
                                f" --config={tempdir}/buildkitd.toml"
                                " --oci-worker=true --oci-worker-binary=/usr/bin/buildkit-runc"
                            ),
                            "HOME": tempdir,
                        },
                        cwd=tempdir,
                        broker_url=current_app.config["CELERY_BROKER_URL"],
                        build_type="release",
                        build_job_id=release.build_job_id,
                        heartbeat_type="release_build",
                        heartbeat_id=str(release.id),
                    )
                except subprocess.CalledProcessError as proc_exc:
                    db.session.refresh(release)
                    release.release_build_log = proc_exc.output
                    db.session.commit()
                    raise BuildError(
                        f"Build subprocess failed with exit code {proc_exc.returncode}"
                    )

            db.session.refresh(release)
            release.release_build_log = output
            db.session.commit()
    except Exception as exc:
        raise BuildError(f"Build failed: {exc}")

    try:
        pushed_release = bke.verify_registry_tag(
            release.repository_name, f"release-{release.version}"
        )
    except Exception as exc:
        raise BuildError(f"Release push failed: {exc}")

    return {
        "release_id": pushed_release,
    }


def _fetch_github_file(
    github_repository="owner/repo", ref="main", access_token=None, filename="Dockerfile"
):
    g = Github(access_token)
    try:
        content_file = g.get_repo(github_repository).get_contents(filename, ref=ref)
        if content_file.encoding == "base64":
            return b64decode(content_file.content).decode()
        return content_file.content
    except GithubException as e:
        if e.status == 404:
            return None
    except UnknownObjectException:
        return None


def _is_imposter_commit(github_repository="owner/repo", *, ref, sha, access_token=None):
    g = Github(access_token)

    try:
        repo = g.get_repo(github_repository)
    except GithubException as e:
        if e.status == 404:
            return None
    except UnknownObjectException:
        return True

    try:
        result = repo.compare(f"refs/heads/{ref}", sha).status
    except GithubException as e:
        if e.status == 404:
            return None
    except UnknownObjectException:
        raise BuildError(f"branch: {ref} does not exist in {github_repository}")

    return not (result == "behind" or result == "identical")


def _fetch_commit_sha_for_ref(
    github_repository="owner/repo", ref="main", access_token=None
):
    g = Github(access_token)
    try:
        sha = g.get_repo(github_repository).get_commit(ref).sha
    except GithubException as e:
        if e.status == 404:
            return None
        raise
    except UnknownObjectException:
        return None

    if _is_imposter_commit(
        github_repository=github_repository, ref=ref, sha=sha, access_token=access_token
    ):
        raise BuildError(
            f"ref: {ref} does not resolve to a valid commit in {github_repository}"
        )

    return sha


def build_cache_pvc_name(app_env):
    """Compute the PVC name for an application-environment's build cache."""
    import hashlib

    application = app_env.application
    name = (
        "build-image-cache-"
        f"{application.project.organization.k8s_identifier}-"
        f"{application.project.k8s_identifier}-"
        f"{application.k8s_identifier}"
    )
    if app_env.k8s_identifier is not None:
        name += f"-{app_env.environment.k8s_identifier}"
    if len(name) > 63:
        suffix = hashlib.sha256(name.encode()).hexdigest()[:8]
        name = name[:54] + "-" + suffix
    return name


def fetch_image_build_cache_volume_claim(core_api_instance, buildable):
    volume_claim_name = build_cache_pvc_name(buildable.application_environment)
    try:
        volume_claim = core_api_instance.read_namespaced_persistent_volume_claim(
            volume_claim_name, "default"
        )
    except ApiException as exc:
        if exc.status == 404:
            volume_claim = core_api_instance.create_namespaced_persistent_volume_claim(
                "default",
                kubernetes.client.V1PersistentVolumeClaim(
                    metadata=kubernetes.client.V1ObjectMeta(
                        name=volume_claim_name,
                    ),
                    spec=kubernetes.client.V1PersistentVolumeClaimSpec(
                        access_modes=["ReadWriteOncePod"],
                        resources=kubernetes.client.V1VolumeResourceRequirements(
                            requests={"storage": "50Gi"},
                        ),
                    ),
                ),
            )
        else:
            raise BuildError(
                f"Unexpected exception fetching PersistentVolumeClaim/{volume_claim_name}: {exc}"
            )
    return volume_claim


def build_image_buildkit(image=None):
    bke = BuildkitEnv(image.repository_name)
    registry = bke.registry
    buildkit_image = bke.buildkit_image
    insecure_reg = bke.insecure_reg
    dockerconfigjson = bke.dockerconfigjson
    buildkitd_toml = bke.buildkitd_toml

    access_token = _fetch_github_access_token(image.application)
    source = _fetch_image_source(image, access_token)
    git_ref = source["git_ref"]
    dockerfile_name = source["dockerfile_name"]
    dockerfile_body = source["dockerfile_body"]
    procfile_body = source["procfile_body"]
    processes = source["processes"]
    dockerfile_env_vars = source["dockerfile_env_vars"]

    buildctl_command = [
        "buildctl-daemonless.sh",
    ]
    buildctl_args = [
        "build",
        "--progress=plain",
        "--frontend",
        "dockerfile.v0",
        "--opt",
        f"filename=./{dockerfile_name}",
        "--opt",
        (f"context={git_ref(image.application.github_repository, image.commit_sha)}"),
        "--import-cache",
        (
            f"type=registry,ref={registry}/{image.repository_name}"
            f":image-buildcache{insecure_reg}"
        ),
        "--export-cache",
        (
            f"type=registry,ref={registry}/{image.repository_name}"
            f":image-buildcache{insecure_reg},mode=min"
        ),
        "--output",
        (
            f"type=image,name={registry}/{image.repository_name}"
            f":image-{image.version},push=true{insecure_reg}"
        ),
    ]

    buildctl_args.append("--opt")
    buildctl_args.append(shlex.quote(f"build-arg:SOURCE_COMMIT={image.commit_sha}"))

    for k, v in image.buildargs(config_writer).items():
        buildctl_args.append("--opt")
        buildctl_args.append(shlex.quote(f"build-arg:{k}={v}"))

    buildctl_args += bke.tls_context_args()

    try:
        if current_app.config["KUBERNETES_ENABLED"]:
            api_client = kubernetes_ext.kubernetes_client
            core_api_instance = kubernetes.client.CoreV1Api(api_client)
            batch_api_instance = kubernetes.client.BatchV1Api(api_client)
            # Create PersistentVolumeClaim
            volume_claim = fetch_image_build_cache_volume_claim(
                core_api_instance, image
            )
            if image.application.github_repository_is_private:
                buildctl_args.append("--secret")
                buildctl_args.append(
                    "id=GIT_AUTH_TOKEN,src=/home/user/.secret/github_access_token"
                )
            docker_secret_object = kubernetes.client.V1Secret(
                type="kubernetes.io/dockerconfigjson",
                metadata=kubernetes.client.V1ObjectMeta(
                    name=f"buildkit-registry-auth-{image.build_job_id}",
                ),
                data={
                    ".dockerconfigjson": b64encode(dockerconfigjson.encode()).decode(),
                },
            )
            github_secret_object = kubernetes.client.V1Secret(
                metadata=kubernetes.client.V1ObjectMeta(
                    name=f"github-access-token-{image.build_job_id}",
                ),
                data={
                    "github_access_token": b64encode(
                        str(access_token).encode()
                    ).decode(),
                },
            )
            buildkitd_toml_configmap_object = kubernetes.client.V1ConfigMap(
                metadata=kubernetes.client.V1ObjectMeta(
                    name=f"buildkitd-toml-{image.build_job_id}",
                ),
                data={
                    "buildkitd.toml": buildkitd_toml,
                },
            )
            job_object = kubernetes.client.V1Job(
                metadata=kubernetes.client.V1ObjectMeta(
                    name=f"imagebuild-{image.build_job_id}",
                    labels={
                        "organization": image.application.project.organization.slug,
                        "project": image.application.project.slug,
                        "application": image.application.slug,
                        "process": "build",
                        "build_id": image.build_job_id,
                        "resident-job.cabotage.io": "true",
                    },
                ),
                spec=kubernetes.client.V1JobSpec(
                    active_deadline_seconds=1800,
                    backoff_limit=0,
                    parallelism=1,
                    completions=1,
                    template=kubernetes.client.V1PodTemplateSpec(
                        metadata=kubernetes.client.V1ObjectMeta(
                            labels={
                                "organization": image.application.project.organization.slug,  # noqa: E501
                                "project": image.application.project.slug,
                                "application": image.application.slug,
                                "process": "build",
                                "build_id": image.build_job_id,
                                "ca-admission.cabotage.io": "true",
                                "resident-pod.cabotage.io": "true",
                            },
                            annotations={
                                "container.apparmor.security.beta.kubernetes.io/build": "unconfined",  # noqa: E501
                            },
                        ),
                        spec=kubernetes.client.V1PodSpec(
                            restart_policy="Never",
                            termination_grace_period_seconds=0,
                            security_context=kubernetes.client.V1PodSecurityContext(
                                fs_group=1000,
                                fs_group_change_policy="OnRootMismatch",
                            ),
                            containers=[
                                kubernetes.client.V1Container(
                                    name="build",
                                    image=buildkit_image,
                                    command=buildctl_command,
                                    args=buildctl_args,
                                    env=[
                                        kubernetes.client.V1EnvVar(
                                            name="BUILDKITD_FLAGS",
                                            value="--config /home/user/.config/buildkit/buildkitd.toml --oci-worker-no-process-sandbox",  # noqa: E501
                                        ),
                                    ],
                                    security_context=kubernetes.client.V1SecurityContext(
                                        seccomp_profile=kubernetes.client.V1SeccompProfile(
                                            type="Unconfined",
                                        ),
                                        run_as_user=1000,
                                        run_as_group=1000,
                                    ),
                                    volume_mounts=[
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/home/user/.config/buildkit",
                                            name="buildkitd-toml",
                                        ),
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/home/user/.docker",
                                            name="buildkit-registry-auth",
                                        ),
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/home/user/.secret",
                                            name="build-secrets",
                                        ),
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/home/user/.local/share/buildkit",
                                            name="build-cache",
                                        ),
                                    ],
                                ),
                            ],
                            volumes=[
                                kubernetes.client.V1Volume(
                                    name="buildkitd-toml",
                                    config_map=kubernetes.client.V1ConfigMapVolumeSource(
                                        name=f"buildkitd-toml-{image.build_job_id}",
                                        items=[
                                            kubernetes.client.V1KeyToPath(
                                                key="buildkitd.toml",
                                                path="buildkitd.toml",
                                            ),
                                        ],
                                    ),
                                ),
                                kubernetes.client.V1Volume(
                                    name="buildkit-registry-auth",
                                    secret=kubernetes.client.V1SecretVolumeSource(
                                        secret_name=f"buildkit-registry-auth-{image.build_job_id}",
                                        items=[
                                            kubernetes.client.V1KeyToPath(
                                                key=".dockerconfigjson",
                                                path="config.json",
                                            ),
                                        ],
                                    ),
                                ),
                                kubernetes.client.V1Volume(
                                    name="build-secrets",
                                    secret=kubernetes.client.V1SecretVolumeSource(
                                        secret_name=f"github-access-token-{image.build_job_id}",
                                        items=[
                                            kubernetes.client.V1KeyToPath(
                                                key="github_access_token",
                                                path="github_access_token",
                                            ),
                                        ],
                                    ),
                                ),
                                kubernetes.client.V1Volume(
                                    name="build-cache",
                                    persistent_volume_claim=kubernetes.client.V1PersistentVolumeClaimVolumeSource(
                                        claim_name=volume_claim.metadata.name
                                    ),
                                ),
                            ],
                        ),
                    ),
                ),
            )

            core_api_instance.create_namespaced_config_map(
                "default", buildkitd_toml_configmap_object
            )
            core_api_instance.create_namespaced_secret("default", docker_secret_object)
            core_api_instance.create_namespaced_secret("default", github_secret_object)

            try:
                redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
                log_key = stream_key("image", image.build_job_id)
            except Exception:  # nosec B110
                redis_client = None
                log_key = None

            try:
                job_complete, job_logs = run_job(
                    core_api_instance,
                    batch_api_instance,
                    "default",
                    job_object,
                    redis_client=redis_client,
                    log_key=log_key,
                    heartbeat_type="image_build",
                    heartbeat_id=str(image.id),
                )
                if redis_client and log_key:
                    try:
                        publish_end(redis_client, log_key, error=not job_complete)
                    except Exception:  # nosec B110
                        pass
            finally:
                core_api_instance.delete_namespaced_secret(
                    f"buildkit-registry-auth-{image.build_job_id}",
                    "default",
                    propagation_policy="Foreground",
                )
                core_api_instance.delete_namespaced_secret(
                    f"github-access-token-{image.build_job_id}",
                    "default",
                    propagation_policy="Foreground",
                )
                core_api_instance.delete_namespaced_config_map(
                    f"buildkitd-toml-{image.build_job_id}",
                    "default",
                    propagation_policy="Foreground",
                )

            db.session.refresh(image)
            image.image_build_log = job_logs
            db.session.commit()
            if not job_complete:
                raise BuildError("Image build failed!")
        else:
            if image.application.github_repository_is_private:
                buildctl_args.append("--secret")
                buildctl_args.append(
                    "id=GIT_AUTH_TOKEN,src=.secret/github_access_token"
                )
            with TemporaryDirectory() as tempdir:
                os.makedirs(os.path.join(tempdir, ".docker"), exist_ok=True)
                with open(os.path.join(tempdir, ".docker", "config.json"), "w") as f:
                    f.write(dockerconfigjson)
                os.makedirs(os.path.join(tempdir, ".secret"), exist_ok=True)
                if (
                    image.application.github_repository_is_private
                    and access_token is not None
                ):
                    with open(
                        os.path.join(tempdir, ".secret", "github_access_token"), "w"
                    ) as f:
                        f.write(access_token)
                with open(os.path.join(tempdir, "buildkitd.toml"), "w") as f:
                    f.write(buildkitd_toml)

                buildkit_root = f"/tmp/buildkit-{image.application.id}-{image.application_environment_id or 'base'}"  # nosec B108 — deterministic path scoped by app+env ID
                os.makedirs(buildkit_root, exist_ok=True)
                sock_addr = f"unix://{buildkit_root}/buildkitd.sock"
                wrapper = os.path.join(tempdir, "buildctl-daemonless.sh")
                with open(wrapper, "w") as f:
                    f.write(
                        "#!/bin/sh\n"
                        "set -eu\n"
                        f"buildkitd --addr={sock_addr} $BUILDKITD_FLAGS &\n"
                        "pid=$!\n"
                        'trap "kill $pid || true; wait $pid || true" EXIT\n'
                        "try=0; max=10\n"
                        f"until buildctl --addr={sock_addr} debug workers >/dev/null 2>&1; do\n"
                        "  if [ $try -gt $max ]; then\n"
                        f'    echo >&2 "could not connect to {sock_addr} after $max trials"\n'
                        "    exit 1\n"
                        "  fi\n"
                        "  sleep 0.1\n"
                        "  try=$((try + 1))\n"
                        "done\n"
                        f'buildctl --addr={sock_addr} "$@"\n'
                    )
                os.chmod(
                    wrapper, 0o755
                )  # nosec B103 — wrapper script must be executable
                buildctl_command = [wrapper]

                try:
                    output = run_and_stream(
                        buildctl_command + buildctl_args,
                        env={
                            **os.environ,
                            "BUILDKITD_FLAGS": (
                                f"--root={buildkit_root}"
                                f" --config={tempdir}/buildkitd.toml"
                                " --oci-worker=true --oci-worker-binary=/usr/bin/buildkit-runc"
                            ),
                            "HOME": tempdir,
                        },
                        cwd=tempdir,
                        broker_url=current_app.config["CELERY_BROKER_URL"],
                        build_type="image",
                        build_job_id=image.build_job_id,
                        heartbeat_type="image_build",
                        heartbeat_id=str(image.id),
                    )
                except subprocess.CalledProcessError as proc_exc:
                    db.session.refresh(image)
                    image.image_build_log = proc_exc.output
                    db.session.commit()
                    raise BuildError(
                        f"Build subprocess failed with exit code {proc_exc.returncode}"
                    )

            db.session.refresh(image)
            image.image_build_log = output
            db.session.commit()
    except Exception as exc:
        raise BuildError(f"Build failed: {exc}")

    try:
        pushed_image = bke.verify_registry_tag(
            image.repository_name, f"image-{image.version}"
        )
    except Exception as exc:
        raise BuildError(f"Image push failed: {exc}")

    return {
        "image_id": pushed_image,
        "processes": processes,
        "dockerfile": dockerfile_body,
        "procfile": procfile_body,
        "dockerfile_env_vars": dockerfile_env_vars,
    }


def build_omnibus_buildkit(image, release):
    """Build image and release in a single K8s Job.

    Uses an init container for the image build and the main container for the
    release build so the build-cache PVC is only mounted/unmounted once.
    """
    bke = BuildkitEnv(image.repository_name)
    registry = bke.registry
    buildkit_image = bke.buildkit_image
    insecure_reg = bke.insecure_reg
    dockerconfigjson = bke.dockerconfigjson
    buildkitd_toml = bke.buildkitd_toml

    access_token = _fetch_github_access_token(image.application)
    source = _fetch_image_source(image, access_token)
    git_ref = source["git_ref"]
    dockerfile_name = source["dockerfile_name"]
    processes = source["processes"]
    dockerfile_env_vars = source["dockerfile_env_vars"]

    # --- Image build args (init container) ---
    buildctl_command = [
        "buildctl-daemonless.sh",
    ]
    image_buildctl_args = [
        "build",
        "--progress=plain",
        "--frontend",
        "dockerfile.v0",
        "--opt",
        f"filename=./{dockerfile_name}",
        "--opt",
        (f"context={git_ref(image.application.github_repository, image.commit_sha)}"),
        "--import-cache",
        (
            f"type=registry,ref={registry}/{image.repository_name}"
            f":image-buildcache{insecure_reg}"
        ),
        "--export-cache",
        (
            f"type=registry,ref={registry}/{image.repository_name}"
            f":image-buildcache{insecure_reg},mode=min"
        ),
        "--output",
        (
            f"type=image,name={registry}/{image.repository_name}"
            f":image-{image.version},push=true{insecure_reg}"
        ),
    ]

    image_buildctl_args.append("--opt")
    image_buildctl_args.append(
        shlex.quote(f"build-arg:SOURCE_COMMIT={image.commit_sha}")
    )

    for k, v in image.buildargs(config_writer).items():
        image_buildctl_args.append("--opt")
        image_buildctl_args.append(shlex.quote(f"build-arg:{k}={v}"))

    image_buildctl_args += bke.tls_context_args()

    if image.application.github_repository_is_private:
        image_buildctl_args.append("--secret")
        image_buildctl_args.append(
            "id=GIT_AUTH_TOKEN,src=/home/user/.secret/github_access_token"
        )

    # --- Prepare release data ---
    # Populate image.processes so that the release can generate its
    # envconsul configurations before the K8s Job actually runs.
    image.processes = processes
    image.built = True
    if image.image_metadata is None:
        image.image_metadata = {"dockerfile_env_vars": dockerfile_env_vars}
    else:
        image.image_metadata["dockerfile_env_vars"] = dockerfile_env_vars
    db.session.add(image)
    db.session.commit()

    # Update the release's image snapshot so envconsul_configurations
    # can see the parsed processes.
    release.image = image.asdict
    db.session.add(release)
    db.session.commit()

    # Generate the release dockerfile and build context
    process_commands = "\n".join(
        [
            (
                f"COPY envconsul-{process_name}.hcl "
                f"/etc/cabotage/envconsul-{process_name}.hcl"
            )
            for process_name in release.envconsul_configurations
        ]
    )
    release.dockerfile = RELEASE_DOCKERFILE_TEMPLATE.format(
        registry=registry,
        image=release.image_snapshot,
        process_commands=process_commands,
    )
    db.session.add(release)
    db.session.commit()

    # --- Release build args (main container) ---
    release_buildctl_args = [
        "build",
        "--progress=plain",
        "--frontend",
        "dockerfile.v0",
        "--output",
        (
            f"type=image,name={registry}/{release.repository_name}"
            f":release-{release.version},push=true{insecure_reg}"
        ),
        "--local",
        "dockerfile=/context",
        "--local",
        "context=/context",
    ]

    release_buildctl_args += bke.tls_context_args()

    # --- Build the single K8s Job ---
    if not current_app.config["KUBERNETES_ENABLED"]:
        raise BuildError("Omnibus build requires KUBERNETES_ENABLED")

    try:
        api_client = kubernetes_ext.kubernetes_client
        core_api_instance = kubernetes.client.CoreV1Api(api_client)
        batch_api_instance = kubernetes.client.BatchV1Api(api_client)
        # Single PVC mount for both build steps
        volume_claim = fetch_image_build_cache_volume_claim(core_api_instance, image)
        docker_secret_object = kubernetes.client.V1Secret(
            type="kubernetes.io/dockerconfigjson",
            metadata=kubernetes.client.V1ObjectMeta(
                name=f"buildkit-registry-auth-{image.build_job_id}",
            ),
            data={
                ".dockerconfigjson": b64encode(dockerconfigjson.encode()).decode(),
            },
        )
        github_secret_object = kubernetes.client.V1Secret(
            metadata=kubernetes.client.V1ObjectMeta(
                name=f"github-access-token-{image.build_job_id}",
            ),
            data={
                "github_access_token": b64encode(str(access_token).encode()).decode(),
            },
        )
        buildkitd_toml_configmap_object = kubernetes.client.V1ConfigMap(
            metadata=kubernetes.client.V1ObjectMeta(
                name=f"buildkitd-toml-{image.build_job_id}",
            ),
            data={
                "buildkitd.toml": buildkitd_toml,
            },
        )
        context_configmap_object = release.release_build_context_configmap
        # Override the configmap name to use image.build_job_id for consistency
        context_configmap_object.metadata.name = f"build-context-{image.build_job_id}"

        shared_env = [
            kubernetes.client.V1EnvVar(
                name="BUILDKITD_FLAGS",
                value="--config /home/user/.config/buildkit/buildkitd.toml --oci-worker-no-process-sandbox",  # noqa: E501
            ),
        ]
        shared_security_context = kubernetes.client.V1SecurityContext(
            seccomp_profile=kubernetes.client.V1SeccompProfile(
                type="Unconfined",
            ),
            run_as_user=1000,
            run_as_group=1000,
        )
        shared_volume_mounts = [
            kubernetes.client.V1VolumeMount(
                mount_path="/home/user/.local/share/buildkit",
                name="build-cache",
            ),
            kubernetes.client.V1VolumeMount(
                mount_path="/home/user/.config/buildkit",
                name="buildkitd-toml",
            ),
            kubernetes.client.V1VolumeMount(
                mount_path="/home/user/.docker",
                name="buildkit-registry-auth",
            ),
        ]

        # Init container: image build
        image_build_container = kubernetes.client.V1Container(
            name="image-build",
            image=buildkit_image,
            command=buildctl_command,
            args=image_buildctl_args,
            env=shared_env,
            security_context=shared_security_context,
            volume_mounts=shared_volume_mounts
            + [
                kubernetes.client.V1VolumeMount(
                    mount_path="/home/user/.secret",
                    name="build-secrets",
                ),
            ],
        )

        # Main container: release build
        release_build_container = kubernetes.client.V1Container(
            name="build",
            image=buildkit_image,
            command=buildctl_command,
            args=release_buildctl_args,
            env=shared_env,
            security_context=shared_security_context,
            volume_mounts=shared_volume_mounts
            + [
                kubernetes.client.V1VolumeMount(
                    mount_path="/context/Dockerfile",
                    sub_path="Dockerfile",
                    name="build-context",
                ),
                kubernetes.client.V1VolumeMount(
                    mount_path="/context/entrypoint.sh",
                    sub_path="entrypoint.sh",
                    name="build-context",
                ),
                *[
                    kubernetes.client.V1VolumeMount(
                        mount_path=f"/context/envconsul-{process_name}.hcl",
                        sub_path=f"envconsul-{process_name}.hcl",
                        name="build-context",
                    )
                    for process_name in release.envconsul_configurations
                ],
            ],
        )

        job_object = kubernetes.client.V1Job(
            metadata=kubernetes.client.V1ObjectMeta(
                name=f"omnibusbuild-{image.build_job_id}",
                labels={
                    "organization": image.application.project.organization.slug,
                    "project": image.application.project.slug,
                    "application": image.application.slug,
                    "process": "build",
                    "build_id": image.build_job_id,
                    "resident-job.cabotage.io": "true",
                },
            ),
            spec=kubernetes.client.V1JobSpec(
                active_deadline_seconds=3600,
                backoff_limit=0,
                parallelism=1,
                completions=1,
                template=kubernetes.client.V1PodTemplateSpec(
                    metadata=kubernetes.client.V1ObjectMeta(
                        labels={
                            "organization": image.application.project.organization.slug,  # noqa: E501
                            "project": image.application.project.slug,
                            "application": image.application.slug,
                            "process": "build",
                            "build_id": image.build_job_id,
                            "ca-admission.cabotage.io": "true",
                            "resident-pod.cabotage.io": "true",
                        },
                        annotations={
                            "container.apparmor.security.beta.kubernetes.io/image-build": "unconfined",  # noqa: E501
                            "container.apparmor.security.beta.kubernetes.io/build": "unconfined",  # noqa: E501
                        },
                    ),
                    spec=kubernetes.client.V1PodSpec(
                        restart_policy="Never",
                        termination_grace_period_seconds=0,
                        security_context=kubernetes.client.V1PodSecurityContext(
                            fs_group=1000,
                            fs_group_change_policy="OnRootMismatch",
                        ),
                        init_containers=[image_build_container],
                        containers=[release_build_container],
                        volumes=[
                            kubernetes.client.V1Volume(
                                name="build-cache",
                                persistent_volume_claim=kubernetes.client.V1PersistentVolumeClaimVolumeSource(
                                    claim_name=volume_claim.metadata.name
                                ),
                            ),
                            kubernetes.client.V1Volume(
                                name="buildkitd-toml",
                                config_map=kubernetes.client.V1ConfigMapVolumeSource(
                                    name=f"buildkitd-toml-{image.build_job_id}",
                                    items=[
                                        kubernetes.client.V1KeyToPath(
                                            key="buildkitd.toml",
                                            path="buildkitd.toml",
                                        ),
                                    ],
                                ),
                            ),
                            kubernetes.client.V1Volume(
                                name="buildkit-registry-auth",
                                secret=kubernetes.client.V1SecretVolumeSource(
                                    secret_name=f"buildkit-registry-auth-{image.build_job_id}",
                                    items=[
                                        kubernetes.client.V1KeyToPath(
                                            key=".dockerconfigjson",
                                            path="config.json",
                                        ),
                                    ],
                                ),
                            ),
                            kubernetes.client.V1Volume(
                                name="build-secrets",
                                secret=kubernetes.client.V1SecretVolumeSource(
                                    secret_name=f"github-access-token-{image.build_job_id}",
                                    items=[
                                        kubernetes.client.V1KeyToPath(
                                            key="github_access_token",
                                            path="github_access_token",
                                        ),
                                    ],
                                ),
                            ),
                            kubernetes.client.V1Volume(
                                name="build-context",
                                config_map=kubernetes.client.V1ConfigMapVolumeSource(
                                    name=f"build-context-{image.build_job_id}"
                                ),
                            ),
                        ],
                    ),
                ),
            ),
        )

        core_api_instance.create_namespaced_config_map(
            "default", buildkitd_toml_configmap_object
        )
        core_api_instance.create_namespaced_config_map(
            "default", context_configmap_object
        )
        core_api_instance.create_namespaced_secret("default", docker_secret_object)
        core_api_instance.create_namespaced_secret("default", github_secret_object)

        try:
            redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
            log_key = stream_key("omnibus", image.build_job_id)
        except Exception:  # nosec B110
            redis_client = None
            log_key = None

        try:
            job_complete, job_logs = run_job(
                core_api_instance,
                batch_api_instance,
                "default",
                job_object,
                redis_client=redis_client,
                log_key=log_key,
                heartbeat_type="omnibus_build",
                heartbeat_id=str(image.id),
            )
            if redis_client and log_key:
                try:
                    publish_end(redis_client, log_key, error=not job_complete)
                except Exception:  # nosec B110
                    pass
        finally:
            core_api_instance.delete_namespaced_secret(
                f"buildkit-registry-auth-{image.build_job_id}",
                "default",
                propagation_policy="Foreground",
            )
            core_api_instance.delete_namespaced_secret(
                f"github-access-token-{image.build_job_id}",
                "default",
                propagation_policy="Foreground",
            )
            core_api_instance.delete_namespaced_config_map(
                f"buildkitd-toml-{image.build_job_id}",
                "default",
                propagation_policy="Foreground",
            )
            core_api_instance.delete_namespaced_config_map(
                f"build-context-{image.build_job_id}",
                "default",
                propagation_policy="Foreground",
            )

        db.session.refresh(image)
        image.image_build_log = job_logs
        db.session.commit()
        db.session.refresh(release)
        release.release_build_log = job_logs
        db.session.commit()
        if not job_complete:
            raise BuildError("Omnibus build failed!")
    except BuildError:
        raise
    except Exception as exc:
        raise BuildError(f"Build failed: {exc}")

    try:
        pushed_image = bke.verify_registry_tag(
            image.repository_name, f"image-{image.version}"
        )
        pushed_release = bke.verify_registry_tag(
            release.repository_name, f"release-{release.version}"
        )
    except Exception as exc:
        raise BuildError(f"Registry verification failed: {exc}")

    return {
        "image_id": pushed_image,
        "release_id": pushed_release,
        "processes": processes,
        "dockerfile": source["dockerfile_body"],
        "procfile": source["procfile_body"],
        "dockerfile_env_vars": dockerfile_env_vars,
    }


@shared_task()
def run_image_build(image_id=None, buildkit=False):
    from cabotage.utils.config_templates import TemplateResolutionError

    current_app.config["REGISTRY_AUTH_SECRET"]
    current_app.config["REGISTRY_BUILD"]
    image = Image.query.filter_by(id=image_id).first()
    if image is None:
        raise KeyError(f"Image with ID {image_id} not found!")

    image.build_job_id = secrets.token_hex(4)

    # Create a GitHub check run at the start of the pipeline
    application = image.application
    check = CheckRun(None, None, application)
    if (
        image.image_metadata
        and "installation_id" in image.image_metadata
        and "sha" in image.image_metadata
        and application.github_repository
    ):
        access_token = github_app.fetch_installation_access_token(
            image.image_metadata["installation_id"]
        )
        app_env = image.application_environment
        env_slug = app_env.environment.slug
        project_slug = application.project.slug
        org_slug = application.project.organization.slug
        check_name = f"deploy - {github_app.slug} / {org_slug} / {project_slug} / {application.slug} ({env_slug})"
        check = CheckRun.create(
            access_token,
            application.github_repository,
            image.image_metadata["sha"],
            check_name,
            application,
            details_url=cabotage_url(application, f"images/{image.id}"),
            app_env=app_env,
        )
        if check.check_run_id:
            metadata = dict(image.image_metadata or {})
            metadata["check_run_id"] = check.check_run_id
            image.image_metadata = metadata

    check.progress(
        "Building image...",
        details_url=cabotage_url(application, f"images/{image.id}"),
        Image=f"images/{image.id}",
    )

    db.session.add(image)
    db.session.commit()

    try:
        redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
        refresh_heartbeat(redis_client, "image_build", str(image.id))
    except Exception:  # nosec B110
        # blind capture any issues sending heartbeat to redis,
        # we don't want to fail the build for this!
        pass

    try:
        try:
            build_metadata = build_image_buildkit(image)
            if (
                image.image_metadata
                and "installation_id" in image.image_metadata
                and "statuses_url" in image.image_metadata
                and not image.image_metadata.get("branch_deploy")
            ):
                access_token = github_app.fetch_installation_access_token(
                    image.image_metadata["installation_id"]
                )
                post_deployment_status_update(
                    access_token,
                    image.image_metadata["statuses_url"],
                    "in_progress",
                    "Image built, Release build commencing.",
                )
        except (BuildError, TemplateResolutionError) as exc:
            db.session.rollback()
            db.session.add(image)
            image.error = True
            image.error_detail = str(exc)
            db.session.commit()
            from cabotage.celery.metrics import record_image_metrics

            record_image_metrics(image)
            if (
                image.image_metadata
                and "installation_id" in image.image_metadata
                and "statuses_url" in image.image_metadata
                and not image.image_metadata.get("branch_deploy")
            ):
                access_token = github_app.fetch_installation_access_token(
                    image.image_metadata["installation_id"]
                )
                post_deployment_status_update(
                    access_token,
                    image.image_metadata["statuses_url"],
                    "failure",
                    "Image build failed.",
                )
            raise
    except Exception:
        try:
            log_key = stream_key("image", image.build_job_id)
            redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
            publish_end(redis_client, log_key, error=True)
        except Exception:  # nosec B110
            pass
        db.session.rollback()
        db.session.add(image)
        if not image.error:
            image.error = True
            image.error_detail = "Image build failed due to an internal error"
            db.session.commit()
        from cabotage.celery.metrics import record_image_metrics

        record_image_metrics(image)
        check.fail(
            "Image build failed",
            detail=image.error_detail or "Image build failed",
            details_url=cabotage_url(application, f"images/{image.id}"),
            Image=f"images/{image.id}",
        )
        raise

    db.session.add(image)
    image.image_id = build_metadata["image_id"]
    image.processes = build_metadata["processes"]
    image.built = True
    image.completed_at = datetime.datetime.utcnow()
    image.image_metadata = {
        **(image.image_metadata or {}),
        "dockerfile_env_vars": build_metadata["dockerfile_env_vars"],
    }

    db.session.add(image)
    db.session.commit()

    from cabotage.celery.metrics import record_image_metrics

    record_image_metrics(image)

    check.progress(
        "Image built",
        detail="Image built successfully. Awaiting release.",
        details_url=cabotage_url(application, f"images/{image.id}"),
        Image=f"images/{image.id}",
    )

    if (
        image.built
        and image.image_metadata
        and image.image_metadata.get("auto_deploy", False)
    ):
        app_env = image.application_environment
        release = image.application.create_release(app_env=app_env)
        release.release_metadata = image.image_metadata
        db.session.add(release)
        db.session.flush()
        activity = Activity(
            verb="create",
            object=release,
            data={
                "user_id": "automation",
                "deployment_id": image.image_metadata.get("id", None),
                "description": image.image_metadata.get("description", None),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()
        check.progress(
            "Building release...",
            detail="Image built, release build starting.",
            details_url=cabotage_url(application, f"releases/{release.id}"),
            Image=f"images/{image.id}",
            Release=f"releases/{release.id}",
        )
        run_release_build.delay(release_id=release.id)


@shared_task()
def run_release_build(release_id=None):
    from cabotage.utils.config_templates import TemplateResolutionError

    release = None
    try:
        current_app.config["REGISTRY_AUTH_SECRET"]
        current_app.config["REGISTRY_BUILD"]
        release = Release.query.filter_by(id=release_id).first()
        if release is None:
            raise KeyError(f"Release with ID {release_id} not found!")

        if release.release_metadata is None:
            release.release_metadata = {}

        release.build_job_id = secrets.token_hex(4)
        db.session.add(release)
        db.session.commit()

        try:
            redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
            refresh_heartbeat(redis_client, "release_build", str(release.id))
        except Exception:  # nosec B110
            # blind capture any issues sending heartbeat to redis,
            # we don't want to fail the build for this!
            pass

        try:
            build_metadata = build_release_buildkit(release)
            release.release_id = build_metadata["release_id"]
            release.built = True
            if (
                "installation_id" in release.release_metadata
                and "statuses_url" in release.release_metadata
                and not release.release_metadata.get("branch_deploy")
            ):
                access_token = github_app.fetch_installation_access_token(
                    release.release_metadata["installation_id"]
                )
                post_deployment_status_update(
                    access_token,
                    release.release_metadata["statuses_url"],
                    "in_progress",
                    "Release built, Deployment commencing.",
                )
        except (BuildError, TemplateResolutionError) as exc:
            db.session.rollback()
            release.error = True
            release.error_detail = str(exc)
            try:
                log_key = stream_key("release", release.build_job_id)
                redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
                publish_end(redis_client, log_key, error=True)
            except Exception:  # nosec B110
                pass
            if (
                "installation_id" in release.release_metadata
                and "statuses_url" in release.release_metadata
                and not release.release_metadata.get("branch_deploy")
            ):
                access_token = github_app.fetch_installation_access_token(
                    release.release_metadata["installation_id"]
                )
                post_deployment_status_update(
                    access_token,
                    release.release_metadata["statuses_url"],
                    "failure",
                    "Release build failed.",
                )
            from cabotage.celery.metrics import record_release_metrics

            record_release_metrics(release)
            CheckRun.from_metadata(
                release.release_metadata, release.application_environment
            ).fail(
                "Release build failed",
                detail=str(exc),
                details_url=cabotage_url(release.application, f"releases/{release.id}"),
                Release=f"releases/{release.id}",
            )
        except Exception:
            db.session.rollback()
            try:
                log_key = stream_key("release", release.build_job_id)
                redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
                publish_end(redis_client, log_key, error=True)
            except Exception:  # nosec B110
                pass
            release.error = True
            release.error_detail = "Release build failed due to an internal error"
            db.session.add(release)
            db.session.commit()
            if (
                "installation_id" in release.release_metadata
                and "statuses_url" in release.release_metadata
                and not release.release_metadata.get("branch_deploy")
            ):
                access_token = github_app.fetch_installation_access_token(
                    release.release_metadata["installation_id"]
                )
                post_deployment_status_update(
                    access_token,
                    release.release_metadata["statuses_url"],
                    "error",
                    "Release build failed.",
                )
            from cabotage.celery.metrics import record_release_metrics

            record_release_metrics(release)
            CheckRun.from_metadata(
                release.release_metadata, release.application_environment
            ).fail(
                "Release build failed",
                detail="Internal error",
                details_url=cabotage_url(release.application, f"releases/{release.id}"),
                Release=f"releases/{release.id}",
            )
            raise

        db.session.add(release)
        db.session.commit()

        if not release.error:
            from cabotage.celery.metrics import record_release_metrics

            record_release_metrics(release)

        image_id = release.image.get("id") if release.image else None
        release_links = {"Release": f"releases/{release.id}"}
        if image_id:
            release_links["Image"] = f"images/{image_id}"
        CheckRun.from_metadata(
            release.release_metadata, release.application_environment
        ).progress(
            "Release built",
            detail="Release built successfully. Awaiting deployment.",
            details_url=cabotage_url(release.application, f"releases/{release.id}"),
            **release_links,
        )

        if (
            release.built
            and release.release_metadata
            and release.release_metadata.get("auto_deploy", False)
        ):
            deployment = Deployment(
                application_id=release.application.id,
                application_environment_id=release.application_environment_id,
                release=release.asdict,
                deploy_metadata=release.release_metadata,
            )
            db.session.add(deployment)
            db.session.flush()
            activity = Activity(
                verb="create",
                object=deployment,
                data={
                    "user_id": "automation",
                    "deployment_id": release.release_metadata.get("id", None),
                    "description": release.release_metadata.get("description", None),
                    "timestamp": datetime.datetime.utcnow().isoformat(),
                },
            )
            db.session.add(activity)
            db.session.commit()
            if current_app.config["KUBERNETES_ENABLED"]:
                deployment_id = deployment.id
                run_deploy.delay(deployment_id=deployment.id)
                deployment = Deployment.query.filter_by(id=deployment_id).first()
            else:
                from cabotage.celery.tasks.deploy import fake_deploy_release

                fake_deploy_release(deployment)
                deployment.complete = True
                db.session.commit()
                CheckRun.from_metadata(
                    release.release_metadata, release.application_environment
                ).succeed(
                    details_url=cabotage_url(
                        release.application,
                        f"deployments/{deployment.id}",
                    ),
                    Deployment=f"deployments/{deployment.id}",
                    Release=f"releases/{release.id}",
                )
    except Exception:
        db.session.rollback()
        if release is not None and not release.error:
            release.error = True
            release.error_detail = "Release build failed due to an internal error"
            db.session.commit()
        raise


@shared_task()
def run_omnibus_build(image_id=None):
    """Build image + release in a single K8s Job for auto-deploys.

    Avoids mounting the build cache volume twice by combining both build
    steps into one pod (init container for image, main container for release).
    """
    from cabotage.utils.config_templates import TemplateResolutionError

    current_app.config["REGISTRY_AUTH_SECRET"]
    current_app.config["REGISTRY_BUILD"]
    image = Image.query.filter_by(id=image_id).first()
    if image is None:
        raise KeyError(f"Image with ID {image_id} not found!")

    image.build_job_id = secrets.token_hex(4)

    # Create a GitHub check run at the start of the pipeline
    application = image.application
    check = CheckRun(None, None, application)
    if (
        image.image_metadata
        and "installation_id" in image.image_metadata
        and "sha" in image.image_metadata
        and application.github_repository
    ):
        access_token = github_app.fetch_installation_access_token(
            image.image_metadata["installation_id"]
        )
        app_env = image.application_environment
        env_slug = app_env.environment.slug
        project_slug = application.project.slug
        org_slug = application.project.organization.slug
        check_name = f"deploy - {github_app.slug} / {org_slug} / {project_slug} / {application.slug} ({env_slug})"
        check = CheckRun.create(
            access_token,
            application.github_repository,
            image.image_metadata["sha"],
            check_name,
            application,
            details_url=cabotage_url(application, f"images/{image.id}"),
            app_env=app_env,
        )
        if check.check_run_id:
            metadata = dict(image.image_metadata or {})
            metadata["check_run_id"] = check.check_run_id
            image.image_metadata = metadata

    check.progress(
        "Building image...",
        details_url=cabotage_url(application, f"images/{image.id}"),
        Image=f"images/{image.id}",
    )

    db.session.add(image)
    db.session.commit()

    try:
        redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
        refresh_heartbeat(redis_client, "omnibus_build", str(image.id))
    except Exception:  # nosec B110
        pass

    release = None
    app_env = image.application_environment
    try:
        try:
            # Create the release record upfront so build_omnibus_buildkit
            # can generate its build context.  Image processes will be
            # populated inside build_omnibus_buildkit before generating the
            # release dockerfile.
            release = Release(
                application_id=application.id,
                application_environment_id=app_env.id,
                image={},  # placeholder, updated inside build_omnibus_buildkit
                _repository_name=application.registry_repository_name(app_env),
                configuration=application._resolved_configuration(app_env),
                image_changes={},
                configuration_changes={},
                ingresses={ing.name: ing.asdict for ing in app_env.ingresses},
                platform=application.platform,
                health_check_path=app_env.effective_health_check_path,
                health_check_host=app_env.effective_health_check_host,
            )
            release.release_metadata = image.image_metadata
            db.session.add(release)
            db.session.flush()

            release.build_job_id = image.build_job_id

            activity = Activity(
                verb="create",
                object=release,
                data={
                    "user_id": "automation",
                    "deployment_id": image.image_metadata.get("id", None),
                    "description": image.image_metadata.get("description", None),
                    "timestamp": datetime.datetime.utcnow().isoformat(),
                },
            )
            db.session.add(activity)
            db.session.commit()

            build_metadata = build_omnibus_buildkit(image, release)

            if (
                image.image_metadata
                and "installation_id" in image.image_metadata
                and "statuses_url" in image.image_metadata
                and not image.image_metadata.get("branch_deploy")
            ):
                access_token = github_app.fetch_installation_access_token(
                    image.image_metadata["installation_id"]
                )
                post_deployment_status_update(
                    access_token,
                    image.image_metadata["statuses_url"],
                    "in_progress",
                    "Release built, Deployment commencing.",
                )
        except (BuildError, TemplateResolutionError) as exc:
            db.session.rollback()
            db.session.add(image)
            image.error = True
            image.error_detail = str(exc)
            if release is not None:
                db.session.add(release)
                release.error = True
                release.error_detail = str(exc)
            db.session.commit()
            if (
                image.image_metadata
                and "installation_id" in image.image_metadata
                and "statuses_url" in image.image_metadata
                and not image.image_metadata.get("branch_deploy")
            ):
                access_token = github_app.fetch_installation_access_token(
                    image.image_metadata["installation_id"]
                )
                post_deployment_status_update(
                    access_token,
                    image.image_metadata["statuses_url"],
                    "failure",
                    "Build failed.",
                )
            raise
    except Exception:
        try:
            log_key = stream_key("omnibus", image.build_job_id)
            redis_client = get_redis_client(current_app.config["CELERY_BROKER_URL"])
            publish_end(redis_client, log_key, error=True)
        except Exception:  # nosec B110
            pass
        db.session.rollback()
        db.session.add(image)
        if not image.error:
            image.error = True
            image.error_detail = "Build failed due to an internal error"
        if release is not None:
            db.session.add(release)
            if not release.error:
                release.error = True
                release.error_detail = "Build failed due to an internal error"
        db.session.commit()
        check.fail(
            "Build failed",
            detail=image.error_detail or "Build failed",
            details_url=cabotage_url(application, f"images/{image.id}"),
            Image=f"images/{image.id}",
        )
        raise

    # Update image record with registry digest
    db.session.add(image)
    image.image_id = build_metadata["image_id"]
    db.session.commit()

    # Update release with image snapshot and registry digest
    db.session.refresh(release)
    release.image = image.asdict
    release.release_id = build_metadata["release_id"]
    release.built = True
    db.session.add(release)
    db.session.commit()

    check.progress(
        "Release built",
        detail="Release built successfully. Deploying.",
        details_url=cabotage_url(application, f"releases/{release.id}"),
        Image=f"images/{image.id}",
        Release=f"releases/{release.id}",
    )

    # Auto-deploy: create deployment
    deployment = Deployment(
        application_id=application.id,
        application_environment_id=app_env.id,
        release=release.asdict,
        deploy_metadata=release.release_metadata,
    )
    db.session.add(deployment)
    db.session.flush()
    activity = Activity(
        verb="create",
        object=deployment,
        data={
            "user_id": "automation",
            "deployment_id": image.image_metadata.get("id", None),
            "description": image.image_metadata.get("description", None),
            "timestamp": datetime.datetime.utcnow().isoformat(),
        },
    )
    db.session.add(activity)
    db.session.commit()
    if current_app.config["KUBERNETES_ENABLED"]:
        run_deploy.delay(deployment_id=deployment.id)
    else:
        from cabotage.celery.tasks.deploy import fake_deploy_release

        fake_deploy_release(deployment)
        deployment.complete = True
        db.session.commit()
        CheckRun.from_metadata(
            release.release_metadata, release.application_environment
        ).succeed(
            details_url=cabotage_url(
                application,
                f"deployments/{deployment.id}",
            ),
            Deployment=f"deployments/{deployment.id}",
            Release=f"releases/{release.id}",
        )
