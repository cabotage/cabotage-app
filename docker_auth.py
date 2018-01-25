import base64
import hashlib

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_public_key,
)


def generate_docker_auth_fingerprint(public_key_pem):
    pub_key = load_pem_public_key(public_key_pem, backend=default_backend())

    der_bytes = pub_key.public_bytes(
        encoding=Encoding.DER,
        format=PublicFormat.SubjectPublicKeyInfo,
    )

    sha256_digest = hashlib.sha256()
    sha256_digest.update(der_bytes)
    b32_digest = base64.b32encode(sha256_digest.digest()[:240 // 8])
    b32_digest_chunks = (
        b32_digest[i:i + 4].decode()
        for i in range(0, len(b32_digest), 4)
    )

    fingerprint = ':'.join(b32_digest_chunks)
    return fingerprint


if __name__ == '__main__':
    import hvac
    client = hvac.Client(token='deadbeef-dead-beef-dead-beefdeadbeef')
    key_data = client.read(
        'cabotage-registry-auth/keys/registry-authentication'
    )['data']
    public_key = key_data['keys']['1']['public_key'].encode()
    print(generate_docker_auth_fingerprint(public_key))
