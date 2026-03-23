"""OIDC issuer utilities for cabotage.

Cabotage acts as an OIDC identity provider, signing JWTs with Vault's
transit engine (ES256/P-256). This is used for Tailscale workload identity
federation and can be extended to other OIDC-aware systems.
"""

import json
import time
import uuid

from base64 import urlsafe_b64encode

from flask import current_app

from cabotage.server import vault
from cabotage.utils.docker_auth import (
    generate_libcrypt_key_id,
    public_key_to_jwk,
)


def issuer_url():
    """Return cabotage's OIDC issuer URL.

    Enforces HTTPS — an HTTP issuer URL would allow MITM of JWKS
    fetches, enabling token forgery.
    """
    scheme = current_app.config.get("EXT_PREFERRED_URL_SCHEME", "https")
    if scheme != "https":
        raise ValueError(
            f"OIDC issuer requires HTTPS but EXT_PREFERRED_URL_SCHEME is {scheme!r}. "
            "Set EXT_PREFERRED_URL_SCHEME=https."
        )
    server = current_app.config["EXT_SERVER_NAME"]
    return f"https://{server}"


def jwks_json():
    """Return the JWKS document containing all active signing key versions.

    Vault transit supports key versioning — when a key is rotated, old
    versions are kept for verification. We include all versions so JWTs
    signed with any active version can be verified during rotation.
    """
    transit_path = f"{vault.vault_signing_mount}/keys/{vault.vault_signing_key}"
    key_data = vault.vault_connection.read(transit_path)
    keys = []
    for _version, key_info in key_data["data"]["keys"].items():
        public_key_pem = key_info["public_key"].encode()
        keys.append(public_key_to_jwk(public_key_pem))
    return json.dumps({"keys": keys})


def _b64url(data):
    """Base64url-encode without padding."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return urlsafe_b64encode(data).rstrip(b"=").decode()


def _jose_header():
    """Build a JWT header using cabotage's signing key."""
    public_key_pem = vault.signing_public_key
    kid = generate_libcrypt_key_id(public_key_pem)
    return json.dumps(
        {"typ": "JWT", "alg": "ES256", "kid": kid},
        separators=(",", ":"),
    )


def mint_jwt(subject, audience, ttl=3600):
    """Mint a signed JWT with the given subject and audience.

    Args:
        subject: The `sub` claim (e.g. "org:myorg")
        audience: The `aud` claim (e.g. "api.tailscale.com/{client_id}")
        ttl: Token lifetime in seconds (default 1 hour)

    Returns:
        A signed JWT string.
    """
    now = int(time.time())
    claims = json.dumps(
        {
            "iss": issuer_url(),
            "sub": subject,
            "aud": audience,
            "exp": now + ttl,
            "nbf": now,
            "iat": now,
            "jti": str(uuid.uuid4()),
        },
        separators=(",", ":"),
    )

    header = _jose_header()
    payload = f"{_b64url(header)}.{_b64url(claims)}"
    signature = vault.sign_payload(payload, marshaling_algorithm="jws")
    return f"{payload}.{signature}"


def mint_tailscale_jwt(org_k8s_identifier, client_id, ttl=3600):
    """Mint a JWT for Tailscale workload identity federation.

    The audience must be api.tailscale.com/{client_id} per Tailscale's
    OIDC federation requirements. The subject uses the org's immutable
    k8s_identifier (not the slug, which can change).
    """
    return mint_jwt(
        subject=f"org:{org_k8s_identifier}",
        audience=f"api.tailscale.com/{client_id}",
        ttl=ttl,
    )
