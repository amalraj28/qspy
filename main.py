"""Small HTTP/1.1 debugging proxy with selective HTTPS interception."""
import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
from pathlib import Path
import select
import socket
import ssl
import threading
import sys
from typing import cast
from urllib.parse import urlsplit
from certifi import where as requests_ca_bundle

from generate_cert import CertificateAuthority

TARGET_DOMAINS = {"example.com"}
LOG = logging.getLogger("custom_mitmproxy")
PRINT_LOCK = threading.Lock()
HOP_HEADERS = {"connection", "proxy-connection", "proxy-authorization", "proxy-authenticate",
               "keep-alive", "te", "trailer", "transfer-encoding", "upgrade"}


def normalize_host(host):
    return host.rstrip(".").encode("idna").decode("ascii").lower()


def log_message(label, url, first_line, headers, body):
    """Decode a copy for display; forwarding retains the original content bytes."""
    from flow_viewer import decode_body
    content = decode_body(headers, body)
    with PRINT_LOCK:
        LOG.info("\n=== %s %s ===\n%s\n%s\n%s\n", label, url, first_line,
                 str(headers).rstrip(), content)


def filtered_headers(headers):
    excluded = HOP_HEADERS | {x.strip().lower() for x in headers.get("Connection", "").split(",")}
    return [(k, v) for k, v in headers.items() if k.lower() not in excluded]


def create_upstream_context(cafile=None):
    """Trust platform roots plus Requests' public bundle and optional private roots."""
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=requests_ca_bundle())
    if cafile:
        context.load_verify_locations(cafile=cafile)
    context.set_alpn_protocols(["http/1.1"])
    return context


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, targets=None, cert_dir=".proxy-certs", upstream_context=None, flow_store=None):
        self.flow_store = flow_store
        self.targets = {normalize_host(h) for h in (TARGET_DOMAINS if targets is None else targets)}
        self.ca = CertificateAuthority(cert_dir)
        self.upstream_context = upstream_context or create_upstream_context()
        self.upstream_context.set_alpn_protocols(["http/1.1"])
        super().__init__(address, ProxyHandler)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30
    # Avoid buffered read-ahead swallowing the start of a CONNECT TLS handshake.
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
            if not host or authority.path:
                raise ValueError("Invalid CONNECT authority")
            if host not in self.proxy_server.targets:
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
                            dest = remote if source is self.connection else self.connection
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
                if url.scheme != "http" or not url.hostname or url.username or url.password or url.fragment:
                    raise ValueError("Expected an absolute http:// proxy URL")
                host, port = normalize_host(url.hostname), url.port or 80
                scheme = "http"
                path = (url.path or "/") + ("?" + url.query if url.query else "")
            body = self.read_body()
            display_url = f"{scheme}://{host}:{port}{path}"
            target = host in self.proxy_server.targets
            if target and self.proxy_server.flow_store is not None:
                flow = self.proxy_server.flow_store.start(self.command, display_url, self.request_version, self.headers, body)
            if target:
                log_message("REQUEST", display_url, f"{self.command} {path} {self.request_version}", self.headers, body)
            if scheme == "https":
                upstream = http.client.HTTPSConnection(host, port, timeout=30, context=self.proxy_server.upstream_context)
            else:
                upstream = http.client.HTTPConnection(host, port, timeout=30)
            upstream.putrequest(self.command, path, skip_host=True, skip_accept_encoding=True)
            authority = f"[{host}]" if ":" in host else host
            if port != (443 if scheme == "https" else 80):
                authority += f":{port}"
            upstream.putheader("Host", authority)
            for key, value in filtered_headers(self.headers):
                if key.lower() not in {"host", "content-length", "expect", "accept-encoding"}:
                    upstream.putheader(key, value)
            upstream.putheader("Accept-Encoding", "gzip, deflate")
            if body or self.headers.get("Content-Length") is not None or self.headers.get("Transfer-Encoding"):
                upstream.putheader("Content-Length", str(len(body)))
            upstream.endheaders(body)
            response = upstream.getresponse()
            if response.status == 101:
                raise ValueError("Protocol upgrades are not supported")
            payload = response.read()
            if flow is not None:
                self.proxy_server.flow_store.finish(flow, response, payload)
            if target:
                log_message("RESPONSE", display_url, f"HTTP/1.1 {response.status} {response.reason}", response.headers, payload)
            self.send_response_only(response.status, response.reason)
            response_started = True
            for key, value in filtered_headers(response.headers):
                if key.lower() != "content-length":
                    self.send_header(key, value)
            if self.command == "HEAD" or response.status == 304:
                if response.headers.get("Content-Length") is not None:
                    self.send_header("Content-Length", response.headers["Content-Length"])
            elif response.status not in (204, 205) and response.status >= 200:
                self.send_header("Content-Length", str(len(payload)))
            elif response.status == 205:
                self.send_header("Content-Length", "0")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (ValueError, OSError, http.client.HTTPException) as exc:
            if flow is not None:
                self.proxy_server.flow_store.finish(flow, error=exc)
            LOG.warning("Proxy request failed: %s", exc)
            if isinstance(exc, ssl.SSLCertVerificationError):
                LOG.warning("Upstream TLS trust failed; for private/corporate issuers, supply their PEM CA bundle with --upstream-ca.")
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
    parser.add_argument("--target", action="append", help="Exact domain to log (repeatable; overrides TARGET_DOMAINS)")
    parser.add_argument("--cert-dir", default=str(Path(__file__).parent / ".proxy-certs"))
    parser.add_argument("--upstream-ca", help="Additional PEM CA bundle for upstream servers (e.g. a corporate CA)")
    parser.add_argument("--log-file", help="Also write plaintext traffic to this file")
    parser.add_argument("--interactive", action="store_true", help="Open terminal flow inspector (Enter to inspect; q to quit)")
    args = parser.parse_args()
    if args.interactive and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        parser.error("--interactive requires an interactive terminal")
    if args.interactive:
        try:
            import curses  # Do not remove!
            from flow_viewer import FlowStore, run_viewer
        except ImportError:
            parser.error("The terminal viewer requires curses. On Windows, run uv sync to install windows-curses.")
    handlers: list[logging.Handler] = [logging.NullHandler()] if args.interactive else [logging.StreamHandler()]
    
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=handlers)
    
    with ProxyServer((args.host, args.port), args.target, args.cert_dir,
                     upstream_context=create_upstream_context(args.upstream_ca),
                     flow_store=FlowStore() if args.interactive else None) as server:
        LOG.info("Proxy listening on %s:%s; targets: %s", *server.server_address, ", ".join(sorted(server.targets)))
        LOG.info("Trust this CA in your client: %s", server.ca.cert_path)
        try:
            if args.interactive:
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    run_viewer(server.flow_store, server.server_address)
                finally:
                    server.shutdown()
                    worker.join()
            else:
                server.serve_forever()
        except KeyboardInterrupt:
            LOG.info("Shutting down proxy")

if __name__ == "__main__":
    main()
