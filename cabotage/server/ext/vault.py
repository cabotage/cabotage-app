from __future__ import annotations

import os

from base64 import (
    b64decode,
    b64encode,
)
from typing import cast, Any, overload, Literal, TYPE_CHECKING

import hvac

from flask import g

from cabotage.utils.cert_hacks import construct_cert_from_public_key

if TYPE_CHECKING:
    from cabotage._types.server import TypedFlask


class Vault(object):
    vault_url: str
    vault_verify: bool
    vault_cert: tuple[str, str] | None
    vault_token: str | None
    vault_token_file: str
    vault_token_unwrap: bool
    vault_prefix: str
    vault_signing_mount: str
    vault_signing_key: str

    def __init__(self, app: TypedFlask | None = None) -> None:
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app: TypedFlask) -> None:
        self.vault_url = app.config.get("VAULT_URL", "http://127.0.0.1:8200")
        self.vault_verify = app.config.get("VAULT_VERIFY", False)
        self.vault_cert = app.config.get("VAULT_CERT", None)
        self.vault_token = app.config.get("VAULT_TOKEN", None)
        self.vault_token_file = app.config.get(
            "VAULT_TOKEN_FILE", os.path.expanduser("~/.vault-token")
        )
        self.vault_token_unwrap = app.config.get("VAULT_TOKEN_UNWRAP", False)
        self.vault_prefix = app.config.get("VAULT_PREFIX", "secret/cabotage")
        self.vault_signing_mount = app.config.get("VAULT_SIGNING_MOUNT", "transit")
        self.vault_signing_key = app.config.get("VAULT_SIGNING_KEY", "cabotage-app")

        if self.vault_token is None:
            if os.path.exists(self.vault_token_file):
                with open(self.vault_token_file, "r") as vault_token_file:
                    self.vault_token = vault_token_file.read().lstrip().rstrip()

        # Unwrap!
        # if self.vault_token_unwrap:
        #    unwrap_dang_token

        app.teardown_appcontext(self.teardown)

    def connect_vault(self) -> hvac.Client:
        vault_client = hvac.Client(
            url=self.vault_url,
            token=self.vault_token,
            verify=self.vault_verify,
            cert=self.vault_cert,
        )
        return vault_client

    def teardown(self, exception: BaseException | None) -> None:
        g.pop("vault_client", None)

    @property
    def vault_connection(self) -> hvac.Client:
        if "vault_client" not in g:
            g.vault_client = self.connect_vault()
        return g.vault_client

    @property
    def signing_public_key(self) -> bytes:
        VAULT_TRANSIT_KEY = f"{self.vault_signing_mount}/keys/{self.vault_signing_key}"
        key_data = cast(dict[str, Any], self.vault_connection.read(VAULT_TRANSIT_KEY))
        keys = key_data["data"]["keys"]
        latest = str(key_data["data"]["latest_version"])
        return keys[latest]["public_key"].encode()

    @property
    def signing_cert(self) -> str:
        return construct_cert_from_public_key(
            self.sign_payload,
            self.signing_public_key,
            "cabotage-app",
        )

    @overload
    def sign_payload(
        self,
        payload: str,
        algorithm: str = ...,
        marshaling_algorithm: Literal["asn1"] = ...,
    ) -> bytes: ...

    @overload
    def sign_payload(
        self,
        payload: str,
        algorithm: str = ...,
        # HACK: current call sites are already passing this as a kwarg
        # https://github.com/python/mypy/issues/7333#issuecomment-788255229
        *,
        marshaling_algorithm: Literal["jws"],
    ) -> str: ...

    def sign_payload(
        self,
        payload: str,
        algorithm: str = "sha2-256",
        marshaling_algorithm: Literal["asn1", "jws"] = "asn1",
    ) -> str | bytes:
        if algorithm not in ("sha2-224", "sha2-256", "sha2-384", "sha2-512"):
            raise KeyError(f"Specified algorithm ({algorithm}) not supported!")
        VAULT_TRANSIT_SIGNING = (
            f"{self.vault_signing_mount}/sign/{self.vault_signing_key}/{algorithm}"
        )
        signature_response = cast(
            dict[str, Any],
            self.vault_connection.write(  # type: ignore[missing-argument] # ty: ignore[missing-argument]
                VAULT_TRANSIT_SIGNING,
                input=b64encode(payload.encode()).decode(),
                marshaling_algorithm=marshaling_algorithm,
            ),
        )
        signature = cast(str, signature_response["data"]["signature"]).split(":")[2]
        if marshaling_algorithm == "jws":
            return signature
        return b64decode(signature)
