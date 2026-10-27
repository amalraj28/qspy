"""Persistent local certificate authority and thread-safe leaf certificates."""
import datetime as dt
import hashlib
import ipaddress
import os
from pathlib import Path
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def certificate_time(cert, field):
    """Use timezone-aware APIs, with support for older cryptography releases."""
    try:
        return getattr(cert, field + "_utc")
    except AttributeError:
        return getattr(cert, field).replace(tzinfo=dt.timezone.utc)


class CertificateAuthority:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cert_path = self.directory / "ca.crt"
        self.key_path = self.directory / "ca.key"
        self.lock = threading.Lock()
        self.cache = {}
        if self.cert_path.exists() != self.key_path.exists():
            raise ValueError("CA certificate/key pair is incomplete")
        if self.key_path.exists():
            loaded_key = serialization.load_pem_private_key(self.key_path.read_bytes(), None)
            if not isinstance(loaded_key, rsa.RSAPrivateKey):
                raise ValueError("CA private key must be RSA")
            self.key = loaded_key
            self.cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
            cert_public_key = self.cert.public_key()
            if not isinstance(cert_public_key, rsa.RSAPublicKey):
                raise ValueError("CA certificate public key must be RSA")
            if self.key.public_key().public_numbers() != cert_public_key.public_numbers():
                raise ValueError("CA certificate and key do not match")
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Custom MITM Proxy CA")])
            now = dt.datetime.now(dt.timezone.utc)
            self.cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                         .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                         .not_valid_before(now - dt.timedelta(minutes=5))
                         .not_valid_after(now + dt.timedelta(days=3650))
                         .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
                         .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
                         .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), False)
                         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), False)
                         .sign(self.key, hashes.SHA256()))
            self._write_key(self.key_path, self.key)
            self.cert_path.write_bytes(self.cert.public_bytes(serialization.Encoding.PEM))

        # Upgrade certificates created by the original implementation while
        # retaining the CA identity/key. Clients importing the CA must reimport it.
        try:
            self.cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
            self.cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        except x509.ExtensionNotFound:
            builder = (x509.CertificateBuilder().subject_name(self.cert.subject)
                       .issuer_name(self.cert.issuer).public_key(self.key.public_key())
                       .serial_number(self.cert.serial_number)
                       .not_valid_before(certificate_time(self.cert, "not_valid_before"))
                       .not_valid_after(certificate_time(self.cert, "not_valid_after")))
            for extension in self.cert.extensions:
                if not isinstance(extension.value, (x509.SubjectKeyIdentifier, x509.AuthorityKeyIdentifier)):
                    builder = builder.add_extension(extension.value, extension.critical)
            self.cert = (builder
                         .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), False)
                         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), False)
                         .sign(self.key, hashes.SHA256()))
            self.cert_path.write_bytes(self.cert.public_bytes(serialization.Encoding.PEM))

    @staticmethod
    def _write_key(path, key):
        with open(path, "wb", opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
            file.write(key.private_bytes(serialization.Encoding.PEM,
                       serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    def generate_cert_files(self, hostname):
        with self.lock:
            if hostname in self.cache:
                return self.cache[hostname]
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            try:
                san = x509.IPAddress(ipaddress.ip_address(hostname))
            except ValueError:
                san = x509.DNSName(hostname)
            now = dt.datetime.now(dt.timezone.utc)
            cert = (x509.CertificateBuilder()
                    .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
                    .issuer_name(self.cert.subject).public_key(key.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(now - dt.timedelta(minutes=5))
                    .not_valid_after(min(now + dt.timedelta(days=30), certificate_time(self.cert, "not_valid_after")))
                    .add_extension(x509.SubjectAlternativeName([san]), False)
                    .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                    .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
                    .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), False)
                    .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), True)
                    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
                    .sign(self.key, hashes.SHA256()))
            stem = hashlib.sha256(hostname.encode()).hexdigest()
            cert_path, key_path = self.directory / f"{stem}.crt", self.directory / f"{stem}.key"
            self._write_key(key_path, key)
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            self.cache[hostname] = str(cert_path), str(key_path)
            return self.cache[hostname]
