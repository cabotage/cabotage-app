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
        repository="cabotage/org_project_application",
        type="registry",
        name="catalog",
        actions=None,
        ):
    if actions is None:
        actions = ["*"]

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
        "access": [
            {
                "type": type,
                "name": name,
                "actions": actions,
            },
        ],
    }, separators=(',', ':'))


if __name__ == '__main__':
    VAULT_TOKEN = 'deadbeef-dead-beef-dead-beefdeadbeef'
    VAULT_ADDR = 'http://127.0.0.1:8200'
    VAULT_TRANSIT_KEY = 'cabotage-signing/keys/docker-auth'
    VAULT_TRANSIT_SIGNING = 'cabotage-signing/sign/docker-auth/sha2-256'

    vault_client = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)

    key_data = vault_client.read(VAULT_TRANSIT_KEY)
    keys = key_data['data']['keys']
    latest = str(max((int(x) for x in keys.keys())))
    public_key_pem = keys[latest]['public_key'].encode()

    header = generate_docker_jose_header(public_key_pem)
    claim_set = generate_docker_claim_set()
    payload = (f'{urlsafe_b64encode(header.encode("utf-8")).rstrip(b"=").decode()}'
               f'.{urlsafe_b64encode(claim_set.encode("utf-8")).rstrip(b"=").decode()}')

    signature_response = vault_client.write(
        VAULT_TRANSIT_SIGNING,
        input=b64encode(payload.encode()).decode(),
    )
    signature_encoded = signature_response['data']['signature'].split(':')[2]
    signature_bytes = b64decode(signature_encoded)

    signature = der_to_raw_signature(signature_bytes, ec.SECP256R1)
    jwt = f'{payload}.{urlsafe_b64encode(signature).rstrip(b"=").decode()}'
    print(jwt)
