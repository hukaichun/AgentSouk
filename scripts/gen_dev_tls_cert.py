#!/usr/bin/env python3
"""Generates a self-signed TLS cert+key pair for running funduq with TLS on
localhost during development/testing — see funduq.config's grpc_tls_*/
http_tls_* settings. Not for production: a real deployment needs a
CA-issued certificate (or TLS terminated at a reverse proxy in front of
funduq), since a self-signed cert only works if every client is separately
told to trust this exact file (funduq_agent_sdk.FunduqProvider's
ca_cert_path), which doesn't scale past "everyone building against one
funduq you personally control".

Usage: uv run python scripts/gen_dev_tls_cert.py [output_dir]
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out_dir.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path = out_dir / "dev_cert.pem"
    key_path = out_dir / "dev_key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    print(f"wrote {cert_path} and {key_path} (valid for localhost/127.0.0.1, 365 days)")


if __name__ == "__main__":
    main()
