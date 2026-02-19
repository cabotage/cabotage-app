import json
import jwcrypto
import time
import uuid

from base64 import (
    urlsafe_b64encode,
)

from cabotage.server import vault


def generate_grafana_jose_header(public_key_pem):
    jwk = jwcrypto.jwk.JWK.from_pem(public_key_pem)
    return json.dumps(
        {
            "typ": "JWT",
            "alg": "ES256",
            "kid": jwk.kid,
        },
        separators=(",", ":"),
    )


def generate_grafana_claim_set(
    issuer="cabotage-app",
    subject="cabotage-user",
    audience="grafana",
    access=None,
):
    if access is None:
        access = []

    jti = str(uuid.uuid4())
    issued_at = int(time.time())
    return json.dumps(
        {
            "iss": issuer,
            "sub": subject,
            "aud": audience,
            "exp": int(issued_at + 600),  # Effectively limits builds to 10 minutes
            "nbf": issued_at,
            "iat": issued_at,
            "jti": jti,
            "access": access,
        },
        separators=(",", ":"),
    )


def generate_grafana_jwt(access=None):
    if access is None:
        access = []

    public_key_pem = vault.signing_public_key

    header = generate_grafana_jose_header(public_key_pem)
    claim_set = generate_grafana_claim_set(access=access)
    header_encoded = urlsafe_b64encode(header.encode("utf-8"))
    claim_set_encoded = urlsafe_b64encode(claim_set.encode("utf-8"))
    payload = (
        f'{header_encoded.rstrip(b"=").decode()}'
        f'.{claim_set_encoded.rstrip(b"=").decode()}'
    )

    signature = vault.sign_payload(payload, marshaling_algorithm="jws")
    return f"{payload}.{signature}"
