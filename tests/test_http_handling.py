"""Phase 2: exercise the proxy against local HTTP servers and raw clients."""

import http.client
import argparse
import gzip
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch

import test_cache_keys as fixtures
from proxy_server import config, proxy
from proxy_server.cli import positive_size, positive_timeout


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.command, self.path))
        self.server.request_headers.append(self.headers)
        self.server.body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            self.server.reply(self)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_POST = do_GET
    do_PUT = do_GET

    def log_message(self, *args):
        pass


class HTTPHandlingTests(unittest.TestCase):
    start_server = fixtures.ProxyRequestTests.start_server

    def setUp(self):
        self.origin = self.start_server(Origin)
        self.origin.reply = lambda handler: self.reply(handler)
        self.cache = fixtures.MemoryCache()
        for patcher in [
            patch.object(config, "ORIGIN_URL", f"http://127.0.0.1:{self.origin.server_port}"),
            patch.object(config, "ORIGIN_TIMEOUT", 0.3),
            patch.object(config, "CLIENT_TIMEOUT", 0.3),
            patch.object(config, "MAX_REQUEST_BODY", 128),
            patch.object(config, "MAX_CACHE_BODY", 32),
            patch.object(proxy, "cache", self.cache),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = self.start_server(proxy.ProxyRequestHandler)

    @staticmethod
    def reply(handler, status=200, headers=None, body=b"hello"):
        handler.send_response(status)
        for name, value in (headers if headers is not None else [("Content-Length", str(len(body)))]):
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()

    def request(self, path="/", method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def raw(self, headers, body=b"", shutdown=True):
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as client:
            client.sendall(b"POST / HTTP/1.1\r\nHost: localhost\r\n" + headers + b"\r\n" + body)
            if shutdown:
                client.shutdown(socket.SHUT_WR)
            result = b""
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    return result
                result += chunk

    def test_request_framing_errors_do_not_contact_origin(self):
        cases = [
            (b"Content-Length: -1\r\n", 400),
            (b"Content-Length: nope\r\n", 400),
            (b"Content-Length: 1\r\nContent-Length: 1\r\n", 400),
            (b"Content-Length: 1, 1\r\n", 400),
            (b"Transfer-Encoding: chunked\r\nContent-Length: 0\r\n", 400),
            (b"Transfer-Encoding: chunked\r\n", 501),
            (b"Transfer-Encoding: gzip\r\n", 501),
            (b"Content-Length: 129\r\n", 413),
            (b"Expect: 100-continue\r\n", 417),
        ]
        for headers, status in cases:
            with self.subTest(headers=headers):
                self.assertIn(f" {status} ".encode(), self.raw(headers).split(b"\r\n")[0])
        self.assertEqual(self.origin.requests, [])

    def test_incomplete_and_stalled_uploads(self):
        self.assertIn(b" 400 ", self.raw(b"Content-Length: 5\r\n", b"ab").split(b"\r\n")[0])
        self.assertIn(b" 408 ", self.raw(b"Content-Length: 5\r\n", b"ab", shutdown=False).split(b"\r\n")[0])
        self.assertEqual(self.origin.requests, [])

    def test_post_body_and_recomputed_length(self):
        status, _, _ = self.request(method="POST", body=b"hello", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 200)
        self.assertEqual(self.origin.body, b"hello")
        self.assertEqual(self.origin.request_headers[-1].get("Content-Length"), "5")
        self.assertEqual(self.cache.entries, {})

    def test_small_body_has_correct_length_on_miss_and_hit(self):
        first = self.request()
        second = self.request()
        self.assertEqual((first[1]["X-Cache"], second[1]["X-Cache"]), ("MISS", "HIT"))
        self.assertEqual(first[2], second[2])
        self.assertEqual(second[1]["Content-Length"], "5")

    def test_large_and_unknown_length_bodies_are_not_cached(self):
        body = b"x" * 100000
        for headers in [[("Content-Length", str(len(body)))], []]:
            self.origin.reply = lambda h: self.reply(h, headers=headers, body=body)
            for _ in range(2):
                status, response_headers, result = self.request()
                self.assertEqual((status, result), (200, body))
                self.assertEqual(response_headers["X-Cache"], "MISS")
        self.assertEqual(len(self.origin.requests), 4)
        self.assertEqual(self.cache.entries, {})

    def test_stream_delivers_bytes_before_origin_finishes(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def reply(handler):
            self.reply(handler, headers=[("Content-Length", "100")], body=b"a")
            release.wait(2)
            handler.wfile.write(b"b" * 99)
        self.origin.reply = reply
        with patch.object(config, "ORIGIN_TIMEOUT", 3):
            client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=1)
            try:
                client.request("GET", "/")
                response = client.getresponse()
                self.assertEqual(response.read(1), b"a")
                release.set()
                self.assertEqual(response.read(), b"b" * 99)
            finally:
                release.set()
                client.close()
        self.assertEqual(self.cache.entries, {})

    def test_chunked_origin_is_decoded_and_not_cached(self):
        self.origin.reply = lambda h: self.reply(h, headers=[("Transfer-Encoding", "chunked"),
            ("Trailer", "X-Trailer")], body=b"5\r\nhello\r\n0\r\nX-Trailer: end\r\n\r\n")
        status, headers, body = self.request()
        self.assertEqual((status, body), (200, b"hello"))
        self.assertNotIn("Transfer-Encoding", headers)
        self.assertNotIn("Trailer", headers)
        self.assertEqual(self.cache.entries, {})

    def test_invalid_origin_framing_returns_bad_gateway(self):
        for headers in [
            [("Transfer-Encoding", "gzip")],
            [("Transfer-Encoding", "chunked"), ("Content-Length", "5")],
            [("Content-Length", "5"), ("Content-Length", "5")],
            [("Content-Length", "bad")],
            [("Content-Length", "20")],  # Only five bytes actually arrive.
        ]:
            self.origin.reply = lambda h: self.reply(h, headers=headers)
            self.assertEqual(self.request()[0], 502)
        self.assertEqual(self.cache.entries, {})

    def test_origin_timeout_and_connection_failure(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.origin.reply = lambda h: release.wait(1)
        self.assertEqual(self.request()[0], 504)
        release.set()
        # A bound, non-listening socket supplies a reserved refused port.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            with patch.object(config, "ORIGIN_URL", f"http://127.0.0.1:{reserved.getsockname()[1]}"):
                self.assertIn(self.request()[0], (502, 504))

    def test_redirect_is_returned_without_following(self):
        self.origin.reply = lambda h: self.reply(h, status=302,
            headers=[("Location", "/destination"), ("Content-Length", "0")], body=b"")
        status, headers, _ = self.request()
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/destination")
        self.assertEqual(self.origin.requests, [("GET", "/")])

    def test_origin_error_body_is_preserved(self):
        self.origin.reply = lambda h: self.reply(h, status=404, body=b"missing")
        status, _, body = self.request()
        self.assertEqual((status, body), (404, b"missing"))
        self.assertEqual(self.cache.entries, {})

    def test_connection_specific_headers_are_removed_both_ways(self):
        self.origin.reply = lambda h: self.reply(h, headers=[
            ("Content-Length", "5"), ("Connection", "X-Internal"),
            ("X-Internal", "secret"), ("Keep-Alive", "timeout=10"),
            ("X-Cache", "fake"), ("Content-Type", "text/plain")])
        _, headers, _ = self.request(headers={"Connection": "X-Internal, Accept-Language",
            "X-Internal": "secret", "Accept-Language": "en", "TE": "trailers"})
        sent = self.origin.request_headers[-1]
        for name in ["X-Internal", "Accept-Language", "TE"]:
            self.assertIsNone(sent.get(name))
        self.assertNotIn("X-Internal", headers)
        self.assertNotIn("Keep-Alive", headers)
        self.assertEqual(headers["X-Cache"], "MISS")
        self.assertEqual(headers["Content-Type"], "text/plain")

    def test_body_range_and_conditional_requests_bypass_existing_cache(self):
        self.request()
        for headers, body in [({}, b"abc"), ({"Range": "bytes=0-1"}, None),
                              ({"If-None-Match": '"tag"'}, None)]:
            self.assertEqual(self.request(headers=headers, body=body)[1]["X-Cache"], "MISS")
        self.assertEqual(len(self.origin.requests), 4)

    def test_bodyless_status_does_not_forward_body_or_length(self):
        for status in [204, 304]:
            self.cache.entries.clear()
            self.origin.reply = lambda h: self.reply(h, status=status, headers=[], body=b"")
            result, headers, body = self.request()
            self.assertEqual((result, body), (status, b""))
            self.assertNotIn("Content-Length", headers)

    def test_cli_limits_reject_invalid_values(self):
        for value in ["0", "-1", "nan", "inf"]:
            with self.assertRaises(argparse.ArgumentTypeError):
                positive_timeout(value)
        for value in ["0", "-1"]:
            with self.assertRaises(argparse.ArgumentTypeError):
                positive_size(value)

    def test_failure_after_streaming_closes_without_second_status(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def reply(handler):
            self.reply(handler, headers=[("Content-Length", "100")], body=b"start")
            release.wait(1)
        self.origin.reply = reply
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            client.request("GET", "/")
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            with self.assertRaises(http.client.IncompleteRead) as failure:
                response.read()
            self.assertEqual(failure.exception.partial, b"start")
        finally:
            release.set()
            client.close()
        self.assertEqual(self.cache.entries, {})

    def test_compressed_representation_preserves_encoding_and_bytes(self):
        body = gzip.compress(b"hello")
        self.origin.reply = lambda h: self.reply(h, headers=[
            ("Content-Length", str(len(body))), ("Content-Encoding", "gzip"),
            ("Vary", "Accept-Encoding")], body=body)
        for expected in ["MISS", "HIT"]:
            status, headers, result = self.request(headers={"Accept-Encoding": "gzip"})
            self.assertEqual((status, result), (200, body))
            self.assertEqual(headers["Content-Encoding"], "gzip")
            self.assertEqual(headers["X-Cache"], expected)


if __name__ == "__main__":
    unittest.main()
