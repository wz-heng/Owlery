"""Throwaway self-signed TLS cert for the fake IMAP/SMTP test servers
(mail-connector.md §4.5) — real TLS/STARTTLS handshakes against 127.0.0.1,
no real CA involved. Production connects via `ssl.create_default_context()`;
tests get an equivalent context that additionally trusts this one
self-signed cert, so the same hostname/chain verification code path runs.
"""

from __future__ import annotations

import datetime
import ipaddress
import ssl
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@dataclass
class FakeMailTLS:
    certfile: Path
    keyfile: Path

    def server_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(self.certfile), str(self.keyfile))
        return ctx

    def client_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.certfile))


def generate(tmp_path: Path) -> FakeMailTLS:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        # Self-signed and acting as its own trust anchor: OpenSSL only
        # accepts it as a CA (needed when Python loads it via the
        # SSL_CERT_FILE env var's default-certs path, e2e's only option
        # since there's no in-process monkeypatch there) with CA:true set.
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certfile = tmp_path / "fake-mail-cert.pem"
    keyfile = tmp_path / "fake-mail-key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return FakeMailTLS(certfile=certfile, keyfile=keyfile)
