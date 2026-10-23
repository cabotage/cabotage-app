"""Real ES256 assertions for admin security behavioral tests."""

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from flask.testing import FlaskClient
from webauthn.helpers import encode_cbor
from werkzeug.test import TestResponse

from cabotage.server import db, security
from cabotage.server.models.auth import User, WebAuthn


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


@dataclass
class SigningPasskey:
    private_key: ec.EllipticCurvePrivateKey
    credential_id: bytes
    user_handle: str
    counter: int = 0

    @classmethod
    def register(cls, user: User) -> SigningPasskey:
        private_key = ec.generate_private_key(ec.SECP256R1())
        public = private_key.public_key().public_numbers()
        user.fs_webauthn_user_handle = user.fs_webauthn_user_handle or uuid.uuid4().hex
        credential_id = uuid.uuid4().bytes
        db.session.add(
            WebAuthn(
                user_id=user.id,
                credential_id=credential_id,
                public_key=encode_cbor(
                    {
                        1: 2,
                        3: -7,
                        -1: 1,
                        -2: public.x.to_bytes(32, "big"),
                        -3: public.y.to_bytes(32, "big"),
                    }
                ),
                sign_count=0,
                name="Security test passkey",
                usage="secondary",
                backup_state=False,
                device_type="single_device",
                lastuse_datetime=db.func.now(),
            )
        )
        db.session.commit()
        return cls(private_key, credential_id, user.fs_webauthn_user_handle)

    def sign(
        self,
        options: dict,
        origin: str,
        *,
        uv: bool = True,
        rp_id: str | None = None,
        challenge: str | None = None,
        user_handle: str | None = None,
    ) -> dict:
        self.counter += 1
        client_data = json.dumps(
            {
                "type": "webauthn.get",
                "challenge": challenge or options["publicKey"]["challenge"],
                "origin": origin,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode()
        authenticator_data = (
            hashlib.sha256((rp_id or options["publicKey"]["rpId"]).encode()).digest()
            + bytes([0x05 if uv else 0x01])
            + self.counter.to_bytes(4, "big")
        )
        signature = self.private_key.sign(
            authenticator_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        return {
            "id": _b64(self.credential_id),
            "rawId": _b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64(client_data),
                "authenticatorData": _b64(authenticator_data),
                "signature": _b64(signature),
                "userHandle": _b64((user_handle or self.user_handle).encode()),
            },
        }

    def verify(
        self,
        client: FlaskClient,
        request_id: str | None = None,
        *,
        origin: str | None = None,
        uv: bool = True,
        rp_id: str | None = None,
        challenge: str | None = None,
        user_handle: str | None = None,
    ) -> TestResponse:
        response = client.post(
            "/admin/passkey/options", json={"request_id": request_id}
        )
        assert response.status_code == 200, response.data
        options = response.get_json()
        with client.application.test_request_context():
            expected_origin = security.webauthn_util.origin()
        credential = self.sign(
            options,
            origin or expected_origin,
            uv=uv,
            rp_id=rp_id,
            challenge=challenge,
            user_handle=user_handle,
        )
        return client.post(
            "/admin/passkey/verify",
            json={
                "request_id": options["request_id"],
                "credential": credential,
            },
        )

    def enter(self, client: FlaskClient) -> None:
        response = self.verify(client)
        assert response.status_code == 200, response.data

    def post(
        self, client: FlaskClient, url: str, data: dict | None = None
    ) -> TestResponse:
        response = client.post(url, data=data or {}, headers={"X-Admin-Fetch": "1"})
        assert response.status_code == 428, response.data
        proof = self.verify(
            client, response.get_json()["admin_verification"]["request_id"]
        )
        assert proof.status_code == 200, proof.data
        return client.post(
            url,
            data=data or {},
            headers={"X-Admin-Action": proof.get_json()["action_token"]},
        )
