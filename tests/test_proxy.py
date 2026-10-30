import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import logging
import ssl
import tempfile
import threading
import unittest

from main import ProxyServer, create_upstream_context
from generate_cert import CertificateAuthority, certificate_time
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization


class Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        payload = gzip.compress(b"plaintext response")
        self.send_response(200)
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = io.StringIO()
        handler = logging.StreamHandler(self.log)
        logger = logging.getLogger("custom_mitmproxy")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.proxy = ProxyServer(("127.0.0.1", 0), {"localhost"}, self.tmp.name)
        self.run_server(self.proxy)
        self.origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        self.origin.daemon_threads = True
        self.run_server(self.origin)

    def run_server(self, server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def connection(self):
        conn = http.client.HTTPConnection(*self.proxy.server_address, timeout=5)
        self.addCleanup(conn.close)
        return conn

    def test_http_filtering_and_keepalive(self):
        conn = self.connection()
        for host in ("localhost", "127.0.0.1", "localhost"):
            conn.request("GET", f"http://{host}:{self.origin.server_port}/hello")
            response = conn.getresponse()
            self.assertEqual(gzip.decompress(response.read()), b"plaintext response")
        self.assertEqual(self.log.getvalue().count("=== REQUEST"), 2)
        self.assertIn("plaintext response", self.log.getvalue())
        self.assertNotIn("http://127.0.0.1", self.log.getvalue())

    def test_chunked_post(self):
        conn = self.connection()
        conn.request("POST", f"http://localhost:{self.origin.server_port}/echo",
                     body=iter([b"hello", b" world"]), encode_chunked=True)
        self.assertEqual(conn.getresponse().read(), b"hello world")
        self.assertIn("hello world", self.log.getvalue())

    def test_flow_capture_and_filtering(self):
        from flow_viewer import FlowStore, detail_lines
        self.proxy.flow_store = FlowStore()
        conn = self.connection()
        for host in ("localhost", "127.0.0.1"):
            conn.request("POST", f"http://{host}:{self.origin.server_port}/echo?a=1&a=2&empty=",
                         body="field=hello+world&field=again",
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(conn.getresponse().read(), b"field=hello+world&field=again")
        flows = self.proxy.flow_store.snapshot()
        self.assertEqual(len(flows), 1)
        flow = flows[0]
        self.assertEqual(flow["status"], 200)
        self.assertGreaterEqual(flow["duration"], 0)
        self.assertEqual(flow["query"], [("a", "1"), ("a", "2"), ("empty", "")])
        self.assertEqual(flow["request"]["form"], [("field", "hello world"), ("field", "again")])
        self.assertIn("HTTP/1.1 200 OK", detail_lines(flow))

    def tls_origin(self, hostname="localhost"):
        cert, key = self.proxy.ca.generate_cert_files(hostname)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        origin.daemon_threads = True
        origin.socket = context.wrap_socket(origin.socket, server_side=True)
        self.run_server(origin)
        return origin

    def test_https_interception_and_ca_persistence(self):
        origin = self.tls_origin()
        trust = ssl.create_default_context(cafile=str(self.proxy.ca.cert_path))
        trust.verify_flags |= ssl.VERIFY_X509_STRICT
        self.proxy.upstream_context = trust
        conn = http.client.HTTPSConnection(*self.proxy.server_address, context=trust, timeout=5)
        self.addCleanup(conn.close)
        conn.set_tunnel("localhost", origin.server_port)
        for _ in range(2):
            conn.request("POST", "/echo", body=b"TLS plaintext request")
            self.assertEqual(conn.getresponse().read(), b"TLS plaintext request")
        self.assertEqual(self.log.getvalue().count("=== RESPONSE"), 2)
        self.assertIn("TLS plaintext request", self.log.getvalue())
        reloaded = CertificateAuthority(self.tmp.name)
        self.assertEqual(reloaded.cert_path.read_bytes(), self.proxy.ca.cert_path.read_bytes())

    def test_non_target_connect_is_unlogged(self):
        origin = self.tls_origin("127.0.0.1")
        trust = ssl.create_default_context(cafile=str(self.proxy.ca.cert_path))
        conn = http.client.HTTPSConnection(*self.proxy.server_address, context=trust, timeout=5)
        self.addCleanup(conn.close)
        conn.set_tunnel("127.0.0.1", origin.server_port)
        conn.request("GET", "/")
        self.assertEqual(gzip.decompress(conn.getresponse().read()), b"plaintext response")
        self.assertEqual(self.log.getvalue(), "")

    def test_legacy_ca_is_upgraded_without_changing_key(self):
        ca = self.proxy.ca
        original_key = ca.key_path.read_bytes()
        old = (x509.CertificateBuilder().subject_name(ca.cert.subject)
               .issuer_name(ca.cert.issuer).public_key(ca.key.public_key())
               .serial_number(ca.cert.serial_number)
               .not_valid_before(certificate_time(ca.cert, "not_valid_before"))
               .not_valid_after(certificate_time(ca.cert, "not_valid_after"))
               .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
               .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
               .sign(ca.key, hashes.SHA256()))
        ca.cert_path.write_bytes(old.public_bytes(serialization.Encoding.PEM))
        upgraded = CertificateAuthority(self.tmp.name)
        upgraded.cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        upgraded.cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        self.assertEqual(original_key, upgraded.key_path.read_bytes())
        self.proxy.ca = upgraded
        self.test_https_interception_and_ca_persistence()

    def test_rejected_handshake_has_no_handler_exception(self):
        errors = []
        self.proxy.handle_error = lambda request, address: errors.append(address)
        conn = http.client.HTTPSConnection(*self.proxy.server_address, timeout=5)
        conn.set_tunnel("localhost", 443)
        with self.assertRaises(ssl.SSLCertVerificationError):
            conn.request("GET", "/")
        conn.close()
        # A second request confirms the proxy still serves traffic.
        healthy = self.connection()
        healthy.request("GET", f"http://localhost:{self.origin.server_port}/")
        healthy.getresponse().read()
        self.assertEqual(errors, [])

    def test_additional_upstream_ca(self):
        origin = self.tls_origin()
        self.proxy.upstream_context = create_upstream_context(str(self.proxy.ca.cert_path))
        self.assertEqual(self.proxy.upstream_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(self.proxy.upstream_context.check_hostname)
        trust = ssl.create_default_context(cafile=str(self.proxy.ca.cert_path))
        conn = http.client.HTTPSConnection(*self.proxy.server_address, context=trust, timeout=5)
        self.addCleanup(conn.close)
        conn.set_tunnel("localhost", origin.server_port)
        conn.request("GET", "/")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(gzip.decompress(response.read()), b"plaintext response")

    def test_upstream_tls_verification(self):
        origin = self.tls_origin()
        trust = ssl.create_default_context(cafile=str(self.proxy.ca.cert_path))
        conn = http.client.HTTPSConnection(*self.proxy.server_address, context=trust, timeout=5)
        self.addCleanup(conn.close)
        conn.set_tunnel("localhost", origin.server_port)
        conn.request("GET", "/")
        response = conn.getresponse()
        self.assertEqual(response.status, 502)
        response.read()


if __name__ == "__main__":
    unittest.main()
