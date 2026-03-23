"""OIDC discovery endpoints.

Cabotage serves as an OIDC identity provider. These public endpoints allow
external systems (Tailscale, Kubernetes, etc.) to discover cabotage's signing
keys and verify JWTs issued by cabotage.
"""

from flask import Blueprint, Response, jsonify

from cabotage.utils.oidc import issuer_url, jwks_json

oidc_blueprint = Blueprint("oidc", __name__)


@oidc_blueprint.route("/.well-known/openid-configuration")
def openid_configuration():
    """OIDC discovery document.

    External systems fetch this to find the JWKS URI and verify that the
    issuer URL matches the `iss` claim in JWTs.
    """
    iss = issuer_url()
    return jsonify(
        {
            "issuer": iss,
            "jwks_uri": f"{iss}/.well-known/jwks.json",
            "authorization_endpoint": f"{iss}/oidc/authorize",
            "token_endpoint": f"{iss}/oidc/token",
            "response_types_supported": ["id_token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["ES256"],
        }
    )


@oidc_blueprint.route("/.well-known/jwks.json")
def jwks():
    """JSON Web Key Set containing cabotage's signing public key.

    The private key stays in Vault's transit engine. This endpoint only
    exposes the public key so external systems can verify JWT signatures.
    """
    return Response(
        jwks_json(),
        mimetype="application/json",
        headers={"Cache-Control": "public, max-age=3600"},
    )
