"""Tests for OIDC issuer endpoints and JWT minting."""

import json
from unittest.mock import patch

import pytest

from cabotage.server.wsgi import app as _app

# Two distinct P-256 public keys for testing key rotation
_TEST_PUBLIC_KEY_1 = b"-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAErwRX+zKtHH458D0/QpqksKM+x7R5\nM07F0uRF9QB1DO/wYIXYRhylEaPQ33eQfzaGcYqM5257hAaaivt5Fj5mtA==\n-----END PUBLIC KEY-----\n"
_TEST_PUBLIC_KEY_2 = b"-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEy85IoBnQuCUUnZkIvWbrXJV734UM\nTyu+azbkgCJwDa6KdzegSBdy0lNKnY87rVCV3ERx5GjV7v2Za8Mun05+HQ==\n-----END PUBLIC KEY-----\n"


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["EXT_SERVER_NAME"] = "cabotage.example.com"
    _app.config["EXT_PREFERRED_URL_SCHEME"] = "https"
    with _app.app_context():
        yield _app


@pytest.fixture
def client(app):
    return app.test_client()


class TestOpenIDConfiguration:
    def test_returns_200(self, client):
        resp = client.get("/.well-known/openid-configuration")
        assert resp.status_code == 200

    def test_content_type(self, client):
        resp = client.get("/.well-known/openid-configuration")
        assert resp.content_type.startswith("application/json")

    def test_issuer_matches_config(self, client):
        resp = client.get("/.well-known/openid-configuration")
        data = json.loads(resp.data)
        assert data["issuer"] == "https://cabotage.example.com"

    def test_jwks_uri(self, client):
        resp = client.get("/.well-known/openid-configuration")
        data = json.loads(resp.data)
        assert data["jwks_uri"] == "https://cabotage.example.com/.well-known/jwks.json"

    def test_signing_alg(self, client):
        resp = client.get("/.well-known/openid-configuration")
        data = json.loads(resp.data)
        assert "ES256" in data["id_token_signing_alg_values_supported"]

    def test_required_fields_present(self, client):
        resp = client.get("/.well-known/openid-configuration")
        data = json.loads(resp.data)
        for field in [
            "issuer",
            "jwks_uri",
            "authorization_endpoint",
            "token_endpoint",
            "response_types_supported",
            "subject_types_supported",
            "id_token_signing_alg_values_supported",
        ]:
            assert field in data, f"Missing required field: {field}"


class TestJWKS:
    @pytest.fixture(autouse=True)
    def mock_vault_key(self):
        with patch("cabotage.utils.oidc.vault") as mock_vault:
            mock_vault.signing_public_key = _TEST_PUBLIC_KEY_2
            mock_vault.vault_signing_mount = "transit"
            mock_vault.vault_signing_key = "cabotage-app"
            mock_vault.vault_connection.read.return_value = {
                "data": {
                    "latest_version": 2,
                    "keys": {
                        "1": {"public_key": _TEST_PUBLIC_KEY_1.decode()},
                        "2": {"public_key": _TEST_PUBLIC_KEY_2.decode()},
                    },
                }
            }
            yield

    def test_returns_200(self, client):
        resp = client.get("/.well-known/jwks.json")
        assert resp.status_code == 200

    def test_content_type(self, client):
        resp = client.get("/.well-known/jwks.json")
        assert resp.content_type.startswith("application/json")

    def test_cache_header(self, client):
        resp = client.get("/.well-known/jwks.json")
        assert "max-age=300" in resp.headers.get("Cache-Control", "")

    def test_keys_present(self, client):
        resp = client.get("/.well-known/jwks.json")
        data = json.loads(resp.data)
        assert "keys" in data
        assert len(data["keys"]) == 2  # both key versions from mock

    def test_key_fields(self, client):
        resp = client.get("/.well-known/jwks.json")
        data = json.loads(resp.data)
        key = data["keys"][0]
        assert key["kty"] == "EC"
        assert key["crv"] == "P-256"
        assert key["alg"] == "ES256"
        assert key["use"] == "sig"
        assert "kid" in key
        assert "x" in key
        assert "y" in key

    def test_multiple_versions_have_distinct_kids(self, client):
        resp = client.get("/.well-known/jwks.json")
        data = json.loads(resp.data)
        kids = [k["kid"] for k in data["keys"]]
        assert len(kids) == 2
        assert kids[0] != kids[1]

    def test_multiple_versions_have_distinct_coordinates(self, client):
        resp = client.get("/.well-known/jwks.json")
        data = json.loads(resp.data)
        xs = [k["x"] for k in data["keys"]]
        assert xs[0] != xs[1]
