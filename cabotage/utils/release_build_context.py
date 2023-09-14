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
