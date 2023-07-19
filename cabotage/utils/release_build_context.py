import io
import time

from tarfile import TarInfo, TarFile

import kubernetes

RELEASE_DOCKERFILE_TEMPLATE = """
FROM {registry}/{image.repository_name}:image-{image.version}
COPY --from=hashicorp/envconsul:0.13.1 /bin/envconsul /usr/bin/envconsul
COPY --chown=root:root --chmod=755 entrypoint.sh /entrypoint.sh
{process_commands}
USER nobody
ENTRYPOINT ["/entrypoint.sh"]
CMD []
"""

ENTRYPOINT = """#!/bin/sh

export VAULT_TOKEN=$(cat /var/run/secrets/vault/vault-token)
export CONSUL_TOKEN=$(cat /var/run/secrets/vault/consul-token)

exec "${@}"
"""


def _string_to_io(string):
    file_handler = io.BytesIO()
    file_handler.write(string.encode())
    file_handler.seek(0)
    return file_handler


def _add_string_to_tarfile(tar_obj, filename, string, mode=0o0644, mtime=None):
    file_handler = _string_to_io(string)
    info = TarInfo(filename)
    info.size = file_handler.getbuffer().nbytes
    info.mode = mode
    info.mtime = mtime if mtime is not None else time.time()
    tar_obj.addfile(info, file_handler)


def tarfile_context_for_release(release, dockerfile):
    mtime = time.mktime(release.created.timetuple())
    tar_fh = io.BytesIO()
    with TarFile.open(fileobj=tar_fh, mode="w:gz") as tar:
        _add_string_to_tarfile(
            tar, "entrypoint.sh", ENTRYPOINT, mode=0o0755, mtime=mtime
        )
        for (
            process_name,
            envconsul_configuration,
        ) in release.envconsul_configurations.items():
            _add_string_to_tarfile(
                tar,
                f"envconsul-{process_name}.hcl",
                envconsul_configuration,
                mtime=mtime,
            )
        _add_string_to_tarfile(tar, "Dockerfile", dockerfile, mtime=mtime)

    tar_fh.seek(0)

    return tar_fh


def configmap_context_for_release(release, dockerfile):
    data = {
        "Dockerfile": dockerfile,
        "entrypoint.sh": ENTRYPOINT,
    }
    for (
        process_name,
        envconsul_configuration,
    ) in release.envconsul_configurations.items():
        data[f"envconsul-{process_name}.hcl"] = envconsul_configuration

    return kubernetes.client.V1ConfigMap(
        metadata=kubernetes.client.V1ObjectMeta(
            name=f"build-context-{release.build_job_id}",
        ),
        data=data,
    )
