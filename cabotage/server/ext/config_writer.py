from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from flask import Flask

    from cabotage.server.ext.vault import Vault
    from cabotage.server.ext.consul import Consul
    from cabotage.server.models.projects import Configuration


class KeySlug(TypedDict):
    config_key_slug: str
    build_key_slug: str


class ConfigWriter(object):
    def __init__(
        self,
        app: Flask | None = None,
        consul: Consul | None = None,
        vault: Vault | None = None,
    ):
        self.app = app
        self.consul = consul
        self.vault = vault
        if app is not None:
            self.init_app(app, consul, vault)

    def init_app(self, app: Flask, consul: Consul | None, vault: Vault | None):
        self.consul = consul
        self.vault = vault
        self.consul_prefix: str = app.config.get("CONSUL_PREFIX", "cabotage")
        self.vault_prefix: str = app.config.get("VAULT_PREFIX", "secret/cabotage")

        app.teardown_appcontext(self.teardown)

    def teardown(self, exception: BaseException | None):
        pass

    def _config_path_segment(self, k8s_namespace: str, k8s_resource_prefix: str) -> str:
        return f"/{k8s_namespace}/{k8s_resource_prefix}"

    def write_configuration(
        self,
        k8s_namespace: str,
        k8s_resource_prefix: str,
        configuration: Configuration,
    ) -> KeySlug:
        version = configuration.version_id + 1 if configuration.version_id else 1
        path_segment = self._config_path_segment(k8s_namespace, k8s_resource_prefix)
        if configuration.secret:
            if self.vault is None:
                raise RuntimeError("No Vault extension configured!")
            config_key_name = (
                f"{self.vault_prefix}/automation"
                f"{path_segment}/configuration/"
                f"{configuration.name}/{version}"
            )
            build_key_name = (
                f"{self.vault_prefix}/buildtime"
                f"{path_segment}/configuration/"
                f"{configuration.name}/{version}"
            )
            storage = "vault"
            self.vault.vault_connection.write(
                config_key_name,
                **{configuration.name: configuration.value},
            )
            if configuration.buildtime:
                self.vault.vault_connection.write(
                    build_key_name,
                    **{configuration.name: configuration.value},
                )
        else:
            if self.consul is None:
                raise RuntimeError("No Consul extension configured!")
            config_key_name = (
                f"{self.consul_prefix}"
                f"{path_segment}/configuration/"
                f"{configuration.name}/{version}/{configuration.name}"
            )
            build_key_name = config_key_name
            storage = "consul"
            self.consul.consul_connection.kv.put(config_key_name, configuration.value)
            config_key_name = "/".join(config_key_name.split("/")[:-1])
        return {
            "config_key_slug": f"{storage}:{config_key_name}",
            "build_key_slug": f"{storage}:{build_key_name}",
        }

    def read(self, key_slug: str, build: bool = False, secret: bool = False):
        if secret:
            if self.vault is None:
                raise RuntimeError("No Vault extension configured!")
            return self.vault.vault_connection.read(key_slug)
        if self.consul is None:
            raise RuntimeError("No Consul extension configured!")
        return self.consul.consul_connection.read(key_slug)
