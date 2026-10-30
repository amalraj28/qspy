from email.message import Message
import gzip
from types import SimpleNamespace
import unittest

from flow_viewer import FlowStore, decode_body, detail_lines


class FlowStoreTests(unittest.TestCase):
    def test_pending_error_and_retention(self):
        store = FlowStore(limit=1)
        first = store.start("GET", "http://example.com/", "HTTP/1.1", Message(), b"")
        pending = store.snapshot()[0]
        self.assertIn("Waiting for response…", detail_lines(pending))
        store.finish(first, error=OSError("connection refused"))
        self.assertIsNone(pending["error"])
        self.assertIn("connection refused", detail_lines(store.snapshot()[0]))
        second = store.start("GET", "http://example.com/next", "HTTP/1.1", Message(), b"")
        self.assertEqual([flow["id"] for flow in store.snapshot()], [second["id"]])

    def test_compressed_json_and_duplicate_headers(self):
        store = FlowStore()
        flow = store.start("GET", "http://example.com/", "HTTP/1.1", Message(), b"")
        headers = Message()
        headers["Content-Type"] = "application/json"
        headers["Content-Encoding"] = "gzip"
        headers["Set-Cookie"] = "a=1"
        headers["Set-Cookie"] = "b=2"
        body = gzip.compress(b'{"ok":true}')
        store.finish(flow, SimpleNamespace(status=200, reason="OK", headers=headers), body)
        message = store.snapshot()[0]["response"]
        self.assertEqual(message["body"], '{\n  "ok": true\n}')
        self.assertEqual(message["bytes"], len(body))
        self.assertEqual(message["headers"][-2:], [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])

    def test_invalid_encoding_is_displayable(self):
        headers = Message()
        headers["Content-Encoding"] = "gzip"
        self.assertIn("could not be decoded", decode_body(headers, b"invalid"))
