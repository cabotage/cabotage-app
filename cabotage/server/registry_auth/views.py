"""Docker registry authentication and signing key endpoints."""

from flask import Blueprint, jsonify, make_response, render_template, request

from flask import current_app

from cabotage.server import vault
from cabotage.utils.docker_auth import (
    check_docker_credentials,
    docker_access_intersection,
    generate_docker_registry_jwt,
    parse_docker_scope,
)
from cabotage.utils.oidc import jwks_json

registry_auth_blueprint = Blueprint("registry_auth", __name__)


@registry_auth_blueprint.route("/docker/auth")
def docker_auth():
    secret = current_app.config["REGISTRY_AUTH_SECRET"]
    if request.authorization is None:
        return jsonify({"error": "authorization required"}), 401
    password = request.authorization.password
    scope_params = request.args.getlist("scope")
    scope = " ".join(scope_params) if scope_params else "registry:catalog:*"
    requested_access = parse_docker_scope(scope)
    max_age = None
    if "push" in [
        action for access in requested_access for action in access["actions"]
    ]:
        max_age = 600
    granted_access = check_docker_credentials(password, secret=secret, max_age=max_age)
    if not granted_access:
        return jsonify({"error": "unauthorized"}), 401
    access = docker_access_intersection(granted_access, requested_access)
    return jsonify({"token": generate_docker_registry_jwt(access=access)})


@registry_auth_blueprint.route("/signing-cert", methods=["GET"])
def signing_cert():
    cert = vault.signing_cert
    raw = request.args.get("raw", None)
    if raw is not None:
        response = make_response(cert, 200)
        response.mimetype = "text/plain"
        return response
    return render_template("user/signing_cert.html", signing_certificate=cert)


@registry_auth_blueprint.route("/signing-jwks", methods=["GET"])
def signing_jwks():
    response = make_response(jwks_json(), 200)
    response.mimetype = "application/json"
    return response
