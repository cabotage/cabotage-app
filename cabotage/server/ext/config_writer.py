from typing import TYPE_CHECKING, cast, overload

if TYPE_CHECKING:
    from typing import Literal

    from flask import Flask

    from cabotage._types.consul import ConsulResponse
    from cabotage._types.vault import VaultSecretResponse
    from cabotage.server.ext.consul import Consul
    from cabotage.server.ext.vault import Vault


class ConfigWriter:
    def __init__(
        self,
        app: Flask | None = None,
        consul: Consul | None = None,
        vault: Vault | None = None,
    ) -> None:
        self.app = app
        self.consul = consul
        self.vault = vault
        if app is not None:
            self.init_app(app, consul, vault)

    def init_app(self, app: Flask, consul: Consul | None, vault: Vault | None) -> None:
        self.consul = consul
        self.vault = vault
        self.consul_prefix = app.config.get("CONSUL_PREFIX", "cabotage")
        self.vault_prefix = app.config.get("VAULT_PREFIX", "secret/cabotage")

        _ = app.teardown_appcontext(self.teardown)

    def teardown(self, exception):
        pass

    def _config_path_segment(self, k8s_namespace, k8s_resource_prefix):
        return f"/{k8s_namespace}/{k8s_resource_prefix}"

    def write_configuration(self, k8s_namespace, k8s_resource_prefix, configuration):
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
            _ = self.vault.vault_connection.write(
                config_key_name,
                **{configuration.name: configuration.value},
            )
            if configuration.buildtime:
                _ = self.vault.vault_connection.write(
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

    @overload
    def read(self, key_slug: str) -> ConsulResponse: ...

    @overload
    def read(
        self, key_slug: str, build: bool = ..., *, secret: Literal[False] = ...
    ) -> ConsulResponse: ...

    @overload
    def read(
        self, key_slug: str, build: bool = ..., *, secret: Literal[True]
    ) -> VaultSecretResponse: ...

    def read(
        self, key_slug: str, build: bool = False, *, secret: bool = False
    ) -> ConsulResponse | VaultSecretResponse:
        if secret:
            if self.vault is None:
                raise RuntimeError("No Vault extension configured!")
            return cast(
                "VaultSecretResponse",
                self.vault.vault_connection.read(key_slug),
            )
        if self.consul is None:
            raise RuntimeError("No Consul extension configured!")
        return cast("ConsulResponse", self.consul.consul_connection.kv.get(key_slug))
