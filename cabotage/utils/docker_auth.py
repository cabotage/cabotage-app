import binascii
import hashlib
import json
import time
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from typing import Literal
    from cabotage._types.docker_auth import Access, JWK

import uuid

from base64 import (
    b32encode,
    urlsafe_b64encode,
)


from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_public_key,
)
from itsdangerous import (
    BadData,
    URLSafeTimedSerializer,
)

from cabotage.server import vault


def number_to_bytes(num: int, num_bytes: int) -> bytes:
    padded_hex = "%0*x" % (2 * num_bytes, num)
    big_endian = binascii.a2b_hex(padded_hex.encode("ascii"))
    return big_endian


def generate_libcrypt_key_id(public_key_pem: bytes) -> str:
    pub_key = load_pem_public_key(public_key_pem)

    der_bytes = pub_key.public_bytes(
        encoding=Encoding.DER,
        format=PublicFormat.SubjectPublicKeyInfo,
    )

    sha256_digest = hashlib.sha256()
    sha256_digest.update(der_bytes)
    b32_digest = b32encode(sha256_digest.digest()[: 240 // 8])
    b32_digest_chunks = (
        b32_digest[i : i + 4].decode() for i in range(0, len(b32_digest), 4)
    )

    fingerprint = ":".join(b32_digest_chunks)
    return fingerprint


def public_key_to_jwk(public_key_pem: bytes) -> JWK:
    """Convert a PEM-encoded EC public key to a JWK dict."""
    pub_key = load_pem_public_key(public_key_pem)
    if not isinstance(pub_key, EllipticCurvePublicKey):
        raise TypeError(f"Expected EC public key, got {type(pub_key).__name__}")
    numbers = pub_key.public_numbers()
    x_bytes = number_to_bytes(numbers.x, 32)
    y_bytes = number_to_bytes(numbers.y, 32)
    x_b64 = urlsafe_b64encode(x_bytes).rstrip(b"=").decode()
    y_b64 = urlsafe_b64encode(y_bytes).rstrip(b"=").decode()
    kid = generate_libcrypt_key_id(public_key_pem)
    return {
        "kty": "EC",
        "crv": "P-256",
        "kid": kid,
        "alg": "ES256",
        "use": "sig",
        "x": x_b64,
        "y": y_b64,
    }


def generate_signing_jwks(public_key_pem: bytes) -> str:
    return json.dumps({"keys": [public_key_to_jwk(public_key_pem)]})


def generate_docker_jose_header(public_key_pem: bytes) -> str:
    return json.dumps(
        {
            "typ": "JWT",
            "alg": "ES256",
            "kid": generate_libcrypt_key_id(public_key_pem),
        },
        separators=(",", ":"),
    )


def generate_docker_claim_set(
    issuer: str = "cabotage-app",
    subject: str = "cabotage-builder",
    audience: str = "cabotage-registry",
    access: list[Access] | None = None,
) -> str:
    if access is None:
        access = []

    jti = str(uuid.uuid4())
    issued_at = int(time.time())
    return json.dumps(
        {
            "iss": issuer,
            "sub": subject,
            "aud": audience,
            "exp": issued_at + 600,  # Effectively limits builds to 10 minutes
            "nbf": issued_at,
            "iat": issued_at,
            "jti": jti,
            "access": access,
        },
        separators=(",", ":"),
    )


def _docker_credential_serializer(secret: str | None = None) -> URLSafeTimedSerializer:
    if secret is None:
        raise ValueError("secret must be supplied!")
    serializer = URLSafeTimedSerializer(secret)
    return serializer


def parse_docker_scope(scope_string: str) -> list[Access]:
    scopes: list[Access] = []
    for scope in scope_string.split(" "):
        if len(scope.split(":")) == 3:
            r_type, r_name, r_actions = scope.split(":")
        elif len(scope.split(":")) > 3:
            r_type, r_host, r_port, r_actions = scope.split(":")
            r_name = f"{r_host}:{r_port}"
        else:
            raise Exception("unreachable")
        scopes.append({"type": r_type, "name": r_name, "actions": r_actions.split(",")})
    return scopes


def docker_access_intersection(
    scope0: list[Access], scope1: list[Access]
) -> list[Access]:
    scope_to_action_0 = {f"{x['type']}:{x['name']}": x["actions"] for x in scope0}
    scope_to_action_1 = {f"{x['type']}:{x['name']}": x["actions"] for x in scope1}
    intersection: list[Access] = []
    for key in frozenset(scope_to_action_0.keys()) & frozenset(
        scope_to_action_1.keys()
    ):
        actions = list(
            frozenset(scope_to_action_0[key]) & frozenset(scope_to_action_1[key])
        )
        if actions:
            r_type, r_name = key.split(":", 1)
            intersection.append({"type": r_type, "name": r_name, "actions": actions})
    return intersection


def generate_docker_credentials(
    secret: str | None = None,
    resource_type: Literal["repository", "registry"] = "registry",
    resource_name: str = "catalog",
    resource_actions: list[str] | None = None,
) -> str:
    if resource_actions is None:
        resource_actions = ["*"]
    serializer = _docker_credential_serializer(secret=secret)
    access = [
        {"type": resource_type, "name": resource_name, "actions": resource_actions}
    ]
    return serializer.dumps(access)


def generate_kubernetes_imagepullsecrets(
    secret: str | None,
    registry_urls: list[str] | None = None,
    resource_type: Literal["repository", "registry"] = "registry",
    resource_name: str = "catalog",
    resource_actions: list[str] | None = None,
) -> str:
    if registry_urls is None:
        registry_urls = []
    password = generate_docker_credentials(
        secret,
        resource_type=resource_type,
        resource_name=resource_name,
        resource_actions=resource_actions,
    )
    return json.dumps(
        {
            "auths": {
                url: {"username": "none", "password": password, "email": "none"}
                for url in registry_urls
            }
        }
    )


def check_docker_credentials(
    token: str, secret: str | None = None, max_age: int | None = 60
) -> list[Access]:
    serializer = _docker_credential_serializer(secret=secret)
    try:
        access = cast("list[Access]", serializer.loads(token, max_age=max_age))
        return access
    except BadData:
        return []


def generate_docker_registry_jwt(access: list[Access] | None = None) -> str:
    if access is None:
        access = []

    public_key_pem = vault.signing_public_key

    header = generate_docker_jose_header(public_key_pem)
    claim_set = generate_docker_claim_set(access=access)
    header_encoded = urlsafe_b64encode(header.encode("utf-8"))
    claim_set_encoded = urlsafe_b64encode(claim_set.encode("utf-8"))
    payload = (
        f"{header_encoded.rstrip(b'=').decode()}"
        f".{claim_set_encoded.rstrip(b'=').decode()}"
    )

    signature = vault.sign_payload(payload, marshaling_algorithm="jws")
    return f"{payload}.{signature}"
