"""Phase 1 tests: real local HTTP requests, isolated in-memory cache."""

import http.client
import json
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch

from proxy_server import config, proxy
from server_helpers import running_server


class MemoryCache:
    def __init__(self):
        self.entries = {}

    def get(self, key):
        return self.entries.get(key)

    def set(self, key, status_code, headers, body, timing=None):
        self.entries[key] = {
            "status_code": status_code, "headers": headers, "body": body,
            **(timing or {}),
        }

    def delete(self, key):
        self.entries.pop(key, None)


class OriginHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.command, self.path))
        self.server.request_headers.append(self.headers)
        body = (
            f"{self.server.label}|{self.command}|{self.path}|"
            f"{self.headers.get('Accept-Language', '')}"
        ).encode()
        self.send_response(200)
        for name, value in self.server.response_headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET

    def log_message(self, format, *args):
        pass


class CacheKeyTests(unittest.TestCase):
    def test_key_distinguishes_method_and_complete_uri(self):
        urls = [
            "http://example.test/products?a=1",
            "https://example.test/products?a=1",
            "http://other.test/products?a=1",
            "http://example.test:8080/products?a=1",
            "http://example.test/base/products?a=1",
            "http://example.test/products?a=2",
        ]
        keys = [proxy.ProxyRequestHandler.build_cache_key("GET", url, {}) for url in urls]
        keys.append(proxy.ProxyRequestHandler.build_cache_key("POST", urls[0], {}))
        self.assertEqual(len(keys), len(set(keys)))

    def test_vary_policy(self):
        for headers in [
            [("Vary", "Accept-Language")],
            [("vArY", "Accept-Encoding")],
            [("Vary", "Accept")],
            [("Vary", "Accept-Language, Accept-Encoding")],
            [("Vary", ""), ("vary", "Accept-Language")],
        ]:
            with self.subTest(headers=headers):
                self.assertTrue(proxy.ProxyRequestHandler.can_store_response(200, headers))
        for headers in [
            [("Vary", "*")], [("Vary", "X-Custom")],
            [("Vary", "Accept-Language, X-Custom")],
            [("Vary", "Accept"), ("vary", "*")],
            [("Vary", "Accept Language")],
        ]:
            with self.subTest(headers=headers):
                self.assertFalse(proxy.ProxyRequestHandler.can_store_response(200, headers))
        self.assertTrue(proxy.ProxyRequestHandler.can_store_response(200, []))
        self.assertTrue(proxy.ProxyRequestHandler.can_store_response(200, [("Vary", " \t")]))


class ProxyRequestTests(unittest.TestCase):
    def start_server(self, handler, label=""):
        return self.enterContext(running_server(handler, label))

    def setUp(self):
        self.origin = self.start_server(OriginHandler, "origin-a")
        self.origin_url = f"http://127.0.0.1:{self.origin.server_port}"
        self.cache = MemoryCache()
        for patcher in [
            patch.object(config, "ORIGIN_URL", self.origin_url),
            patch.object(proxy, "cache", self.cache),
            # Ignore machine HTTP proxy settings so all traffic stays local.
            patch.object(urllib.request, "_opener", urllib.request.build_opener(
                urllib.request.ProxyHandler({}))),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = self.start_server(proxy.ProxyRequestHandler)

    def request(self, path="/products", method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.putrequest(method, path, skip_accept_encoding=True)
            fields = headers.items() if isinstance(headers, dict) else (headers or [])
            for name, value in fields:
                connection.putheader(name, value)
            connection.endheaders()
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(response.status, 200)
            return response.getheader("X-Cache"), body
        finally:
            connection.close()

    def test_miss_then_hit(self):
        first = self.request()
        second = self.request()
        self.assertEqual((first[0], second[0]), ("MISS", "HIT"))
        self.assertEqual(first[1], second[1])
        self.assertEqual(len(self.origin.requests), 1)

    def test_different_origins_share_cache_without_sharing_responses(self):
        first = self.request()
        other = self.start_server(OriginHandler, "origin-b")
        with patch.object(config, "ORIGIN_URL", f"http://127.0.0.1:{other.server_port}"):
            second = self.request()
            self.assertEqual(self.request()[0], "HIT")
        self.assertEqual(second[0], "MISS")
        self.assertNotEqual(first[1], second[1])
        self.assertEqual(self.request(), ("HIT", first[1]))
        self.assertEqual(len(other.requests), 1)
        self.assertEqual(len(self.origin.requests), 1)

    def test_paths_and_queries_are_preserved(self):
        paths = [
            "/products", "/other", "/products?page=1", "/products?page=2",
            "/products?a=1&b=2", "/products?b=2&a=1",
            "/products?tag=a&tag=b", "/products?tag=b&tag=a",
            "/products?q=", "/products?q=%2F", "/products?q=/",
            "/products?q=a+b", "/products?q=a%20b",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], "MISS")
                self.assertEqual(self.request(path)[0], "HIT")
        self.assertEqual(self.origin.requests, [("GET", path) for path in paths])

    def test_origin_base_path_is_used_in_forwarding_and_key(self):
        with patch.object(config, "ORIGIN_URL", self.origin_url + "/api/"):
            self.assertEqual(self.request("/products?q=1")[0], "MISS")
            self.assertEqual(self.request("/products?q=1")[0], "HIT")
        self.assertEqual(self.origin.requests, [("GET", "/api/products?q=1")])
        expected = proxy.ProxyRequestHandler.build_cache_key(
            "GET", self.origin_url + "/api/products?q=1", {"accept-encoding": "identity"})
        self.assertIn(expected, self.cache.entries)

    def test_vary_languages_have_separate_cached_responses(self):
        self.origin.response_headers = [("Vary", ""), ("vArY", "Accept-Language")]
        english = self.request("/welcome", headers={"Accept-Language": "en"})
        french = self.request("/welcome", headers={"Accept-Language": "fr"})
        again = self.request("/welcome", headers={"Accept-Language": "en"})
        french_again = self.request("/welcome", headers={"Accept-Language": "fr"})
        self.assertEqual([english[0], french[0], again[0], french_again[0]],
                         ["MISS", "MISS", "HIT", "HIT"])
        self.assertTrue(english[1].endswith(b"|en"))
        self.assertTrue(french[1].endswith(b"|fr"))
        self.assertEqual(english[1], again[1])
        self.assertEqual(french[1], french_again[1])
        self.assertEqual(len(self.origin.requests), 2)
        self.assertEqual(len(self.cache.entries), 2)

    def test_old_key_is_not_reused(self):
        self.cache.set("GET:/products", 200, [], b"old response")
        self.cache.set('v2:' + json.dumps(["GET", self.origin_url + "/products"],
                                        separators=(",", ":")), 200, [], b"old response")
        result = self.request()
        self.assertEqual(result[0], "MISS")
        self.assertNotEqual(result[1], b"old response")
        self.assertEqual(len(self.origin.requests), 1)

    def test_request_bypasses_ignore_existing_entry(self):
        self.request()
        for headers in [
            {"Authorization": "Bearer test"}, {"Cookie": "session=test"},
            {"Cache-Control": "no-cache"}, {"Cache-Control": "no-store"},
        ]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], "MISS")
                self.assertEqual(self.request("/uncached", headers=headers)[0], "MISS")
        self.assertEqual(len(self.origin.requests), 9)
        self.assertEqual(len(self.cache.entries), 1)

    def test_non_get_methods_do_not_read_or_write_cache(self):
        self.request()
        for method in ["POST", "PUT", "DELETE"]:
            with self.subTest(method=method):
                self.assertEqual(self.request(method=method)[0], "MISS")
                self.assertEqual(self.request(method=method)[0], "MISS")
        self.assertEqual(len(self.origin.requests), 7)
        self.assertEqual(len(self.cache.entries), 1)

    def test_unsupported_vary_is_never_stored(self):
        for vary in ["*", "X-Custom", "Accept-Language, X-Custom"]:
            with self.subTest(vary=vary):
                self.origin.response_headers = [("Vary", vary)]
                self.assertEqual(self.request()[0], "MISS")
                self.assertEqual(self.request()[0], "MISS")
        self.assertEqual(len(self.origin.requests), 6)
        self.assertEqual(self.cache.entries, {})

    def test_absent_empty_and_explicit_language_are_distinct(self):
        self.origin.response_headers = [("Vary", "Accept-Language")]
        for headers in [{}, {"Accept-Language": ""}, {"Accept-Language": "en"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], "MISS")
                self.assertEqual(self.request(headers=headers)[0], "HIT")
        self.assertEqual(len(self.origin.requests), 3)
        self.assertEqual(len(self.cache.entries), 3)

    def test_repeated_request_fields_are_combined_and_keyed(self):
        self.origin.response_headers = [("Vary", "ACCEPT-LANGUAGE")]
        first = self.request(headers=[("Accept-Language", "en"), ("accept-language", "fr;q=0.5")])
        combined = self.request(headers={"ACCEPT-LANGUAGE": "en, fr;q=0.5"})
        different = self.request(headers={"Accept-Language": "fr;q=0.5"})
        self.assertEqual([first[0], combined[0], different[0]], ["MISS", "HIT", "MISS"])
        self.assertTrue(first[1].endswith(b"|en, fr;q=0.5"))
        self.assertEqual(first[1], combined[1])
        self.assertEqual(len(self.origin.requests), 2)

    def test_multiple_vary_fields_use_all_preferences(self):
        self.origin.response_headers = [
            ("Vary", "Accept, Accept-Language"), ("vArY", "Accept-Encoding"),
        ]
        preferences = [
            {"Accept": "application/json", "Accept-Language": "en", "Accept-Encoding": "identity"},
            {"Accept": "text/plain", "Accept-Language": "en", "Accept-Encoding": "identity"},
            {"Accept": "application/json", "Accept-Language": "fr", "Accept-Encoding": "identity"},
            {"Accept": "application/json", "Accept-Language": "en", "Accept-Encoding": "gzip"},
        ]
        for headers in preferences:
            self.assertEqual(self.request(headers=headers)[0], "MISS")
            for name, value in headers.items():
                self.assertEqual(self.origin.request_headers[-1].get(name), value)
        for headers in preferences:
            self.assertEqual(self.request(headers=headers)[0], "HIT")
        self.assertEqual(len(self.origin.requests), 4)
        self.assertEqual(len(self.cache.entries), 4)

    def test_encoding_default_matches_actual_forwarded_value(self):
        self.origin.response_headers = [("Vary", "Accept-Encoding")]
        self.assertEqual(self.request()[0], "MISS")
        self.assertEqual(self.origin.request_headers[-1].get("Accept-Encoding"), "identity")
        self.assertEqual(self.request(headers={"Accept-Encoding": "identity"})[0], "HIT")
        self.assertEqual(self.request(headers={"Accept-Encoding": ""})[0], "MISS")
        self.assertEqual(self.origin.request_headers[-1].get("Accept-Encoding"), "")
        self.assertEqual(len(self.origin.requests), 2)

    def test_unrelated_headers_do_not_fragment_cache(self):
        self.origin.response_headers = [("Vary", "Accept-Language")]
        self.assertEqual(self.request(headers={"X-Request-Id": "one"})[0], "MISS")
        self.assertEqual(self.request(headers={"X-Request-Id": "two"})[0], "HIT")
        self.assertEqual(len(self.origin.requests), 1)


if __name__ == "__main__":
    unittest.main()
