import binascii
import hashlib
import json
import time
import uuid

from base64 import (
    b32encode,
    b32decode,
    b64encode,
    b64decode,
    urlsafe_b64encode,
)

import hvac

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_public_key,
    load_pem_private_key,
)
from itsdangerous import (
    BadData,
    URLSafeTimedSerializer,
)

from cabotage.server import vault


def number_to_bytes(num, num_bytes):
    padded_hex = '%0*x' % (2 * num_bytes, num)
    big_endian = binascii.a2b_hex(padded_hex.encode('ascii'))
    return big_endian


def der_to_raw_signature(der_sig, curve):
    num_bits = curve.key_size
    num_bytes = (num_bits + 7) // 8

    r, s = decode_dss_signature(der_sig)

    return number_to_bytes(r, num_bytes) + number_to_bytes(s, num_bytes)


def generate_libcrypt_key_id(public_key_pem):
    pub_key = load_pem_public_key(public_key_pem, backend=default_backend())

    der_bytes = pub_key.public_bytes(
        encoding=Encoding.DER,
        format=PublicFormat.SubjectPublicKeyInfo,
    )

    sha256_digest = hashlib.sha256()
    sha256_digest.update(der_bytes)
    b32_digest = b32encode(sha256_digest.digest()[:240 // 8])
    b32_digest_chunks = (
        b32_digest[i:i + 4].decode()
        for i in range(0, len(b32_digest), 4)
    )

    fingerprint = ':'.join(b32_digest_chunks)
    return fingerprint


def generate_docker_jose_header(public_key_pem):
    return json.dumps({
        "typ": "JWT",
        "alg": "ES256",
        "kid": generate_libcrypt_key_id(public_key_pem),
    }, separators=(',', ':'))


def generate_docker_claim_set(
            issuer="cabotage-app",
            subject="cabotage-builder",
            audience="cabotage-registry",
            access=None,
        ):
    if access is None:
        access = []

    jti = str(uuid.uuid4())
    issued_at = int(time.time())
    return json.dumps({
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "exp": int(issued_at + 600),  # Effectively limits builds to 10 minutes
        "nbf": issued_at,
        "iat": issued_at,
        "jti": jti,
        "access": access,
    }, separators=(',', ':'))


def _docker_credential_serializer(secret=None):
    if secret is None:
        return ValueError('secret must be supplied!')
    serializer = URLSafeTimedSerializer(secret)
    return serializer


def generate_docker_credentials(secret=None, resource_type="registry", resource_name="catalog", resource_actions=None):
    if resource_actions is None:
        resource_actions = ["*"]
    serializer = _docker_credential_serializer(secret=secret)
    access = [{"type": resource_type, "name": resource_name, "actions": resource_actions}]
    return serializer.dumps(access)


def check_docker_credentials(token, secret=None, max_age=60):
    serializer = _docker_credential_serializer(secret=secret)
    try:
        access = serializer.loads(token, max_age=max_age)
        return access
    except BadData:
        return []


def generate_docker_registry_jwt(access=None):
    if access is None:
        access = []

    public_key_pem = vault.signing_public_key

    header = generate_docker_jose_header(public_key_pem)
    claim_set = generate_docker_claim_set(access=access)
    header_encoded = urlsafe_b64encode(header.encode("utf-8"))
    claim_set_encoded = urlsafe_b64encode(claim_set.encode("utf-8"))
    payload = (f'{header_encoded.rstrip(b"=").decode()}'
               f'.{claim_set_encoded.rstrip(b"=").decode()}')

    signature_bytes = vault.sign_payload(payload)
    signature = der_to_raw_signature(signature_bytes, ec.SECP256R1)
    return f'{payload}.{urlsafe_b64encode(signature).rstrip(b"=").decode()}'
