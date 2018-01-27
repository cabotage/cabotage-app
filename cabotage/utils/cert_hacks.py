import base64
import datetime

import hvac

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def issue_dummy_cert(public_key_pem, common_name):
    """A kind courtesy of @reaperhulk
    """
    discarding_private_key = ec.generate_private_key(
        curve=ec.SECP256R1(), backend=default_backend()
    )

    public_key = serialization.load_pem_public_key(
        public_key_pem,
        backend=default_backend(),
    )

    one_day = datetime.timedelta(1, 0, 0)
    one_year = datetime.timedelta(365, 0, 0)

    builder = x509.CertificateBuilder()
    builder = builder.subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ]))
    builder = builder.issuer_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ]))
    builder = builder.not_valid_before(datetime.datetime.today() - one_day)
    builder = builder.not_valid_after(datetime.datetime.today() + one_year)
    builder = builder.serial_number(x509.random_serial_number())
    builder = builder.public_key(public_key)
    certificate = builder.sign(
        private_key=discarding_private_key,
        algorithm=hashes.SHA256(),
        backend=default_backend(),
    )
    return certificate


def certificate_squisher(cert, signature):
    """A kind courtesy of @reaperhulk

    Function assumes cert is a parsed cryptography x509 cert and that the
    new signature is of the same type as the one being replaced. Returns a
    DER encoded certificate.
    """
    cert_bytes = bytearray(cert.public_bytes(serialization.Encoding.DER))
    # Fix the BITSTRING length
    cert_bytes[-len(cert.signature) - 2] = len(signature) + 1
    # Fix the SEQUENCE length
    cert_bytes[3] += len(signature) - len(cert.signature)
    return bytes(cert_bytes)[:-len(cert.signature)] + signature


def construct_cert_from_public_key(signer, public_key_pem, common_name):
    dummy_cert = issue_dummy_cert(public_key_pem, common_name)
    bytes_to_sign = dummy_cert.tbs_certificate_bytes

    signature_bytes = signer(bytes_to_sign)

    final_certificate_bytes = certificate_squisher(dummy_cert, signed_bytes)
    final_cert = x509.load_der_x509_certificate(
        final_certificate_bytes,
        backend=default_backend(),
    )
    return final_cert.public_bytes(
        encoding=serialization.Encoding.PEM,
    ).decode()
