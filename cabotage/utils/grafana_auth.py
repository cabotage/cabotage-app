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
    user,
    issuer="cabotage-app",
    audience="grafana",
    access=None,
    target_org_id=None
):
    if access is None:
        access = []

    if target_org_id is None:
        raise ValueError("target_org_id must be specified")

    user_role = next(
        (
            "Admin" if org_member.admin else "Viewer"
            for org_member in user.organizations
            if (
                org_member.organization.grafana_org_id == target_org_id
                and org_member.organization.grafana_org_id is not None
            )
        ),
        "Viewer",
    )

    jti = str(uuid.uuid4())
    issued_at = int(time.time())
    return json.dumps(
        {
            "iss": issuer,
            "sub": user.email,
            "name": user.username,
            "email": user.email,
            "login": user.email,
            "aud": audience,
            "exp": int(issued_at + 600),
            "nbf": issued_at,
            "iat": issued_at,
            "jti": jti,
            "role": user_role,
            "access": access,
            "orgId": str(target_org_id)
        },
        separators=(",", ":"),
    )


def generate_grafana_jwt(user=None, access=None, target_org_id=None):
    if access is None:
        access = []

    public_key_pem = vault.signing_public_key

    header = generate_grafana_jose_header(public_key_pem)
    claim_set = generate_grafana_claim_set(user, access=access, target_org_id=target_org_id)
    header_encoded = urlsafe_b64encode(header.encode("utf-8"))
    claim_set_encoded = urlsafe_b64encode(claim_set.encode("utf-8"))
    payload = (
        f'{header_encoded.rstrip(b"=").decode()}'
        f'.{claim_set_encoded.rstrip(b"=").decode()}'
    )

    signature = vault.sign_payload(payload, marshaling_algorithm="jws")
    return f"{payload}.{signature}"
