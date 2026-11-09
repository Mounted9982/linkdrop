from __future__ import annotations

import hashlib
import os
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _certificate_paths(directory: Path) -> tuple[Path, Path]:
    return directory / "device.key", directory / "device.crt"


def ensure_certificate(directory: Path) -> tuple[Path, Path]:
    """Create a persistent self-signed TLS identity for this installation."""
    key_path, cert_path = _certificate_paths(directory)
    if key_path.exists() and cert_path.exists():
        return key_path, cert_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "LinkDrop"),
            x509.NameAttribute(NameOID.COMMON_NAME, "LinkDrop Local Device"),
        ]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    temp_key = key_path.with_suffix(".key.tmp")
    temp_cert = cert_path.with_suffix(".crt.tmp")
    temp_key.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    temp_cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    os.replace(temp_key, key_path)
    os.replace(temp_cert, cert_path)
    return key_path, cert_path


def server_ssl_context(directory: Path) -> ssl.SSLContext:
    key_path, cert_path = ensure_certificate(directory)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


def client_ssl_context() -> ssl.SSLContext:
    # LinkDrop uses an app-managed identity. HTTPS protects the pairing code and
    # payload from passive disclosure; the device identity is shown in the UI.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def certificate_fingerprint(cert_path: str | Path) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    certificate = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    der = certificate.public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))

