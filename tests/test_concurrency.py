import contextlib
import io
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

import test_http_handling as http_tests
from proxy_server import cli, config, proxy


class ConcurrencyTests(unittest.TestCase):
    start_server = http_tests.HTTPHandlingTests.start_server
    request = http_tests.HTTPHandlingTests.request
    reply = staticmethod(http_tests.HTTPHandlingTests.reply)

    def setUp(self):
        http_tests.HTTPHandlingTests.setUp(self)
        self.enterContext(patch.object(config, "ORIGIN_TIMEOUT", 3))

    def simultaneous_misses(self, count):
        # Freeze each client's first lookup AFTER it reads the cache. Every
        # client thus observes the same cold entry regardless of thread timing.
        barrier = threading.Barrier(count, timeout=3)
        local = threading.local()
        original_get = self.cache.get

        def get(key):
            result = original_get(key)
            if not getattr(local, "started", False):
                local.started = True
                barrier.wait()
            return result

        with patch.object(self.cache, "get", side_effect=get):
            with ThreadPoolExecutor(max_workers=count) as pool:
                futures = [pool.submit(self.request) for _ in range(count)]
                return [future.result(timeout=5) for future in futures]

    def test_four_cold_clients_share_one_origin_fetch(self):
        responses = self.simultaneous_misses(4)
        self.assertEqual(self.origin.requests, [("GET", "/")])
        self.assertCountEqual([headers["X-Cache"] for _, headers, _ in responses],
                              ["MISS", "HIT", "HIT", "HIT"])
        for status, _, body in responses:
            self.assertEqual((status, body), (200, b"hello"))
        self.assertFalse(proxy._key_locks)

    def test_variants_can_fetch_in_parallel(self):
        barrier = threading.Barrier(2, timeout=2)

        def reply(handler):
            language = handler.headers["Accept-Language"].encode()
            barrier.wait()
            self.reply(handler, body=language, headers=[
                ("Content-Length", str(len(language))), ("Vary", "Accept-Language")])

        self.origin.reply = reply
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.request, headers={"Accept-Language": lang})
                       for lang in ["en", "fr"]]
            self.assertEqual([future.result()[2] for future in futures], [b"en", b"fr"])
        self.assertEqual(len(self.origin.requests), 2)
        self.assertFalse(proxy._key_locks)

    def test_failed_leader_allows_waiting_request_to_retry(self):
        def reply(handler):
            if len(self.origin.requests) == 1:
                self.reply(handler, body=b"x", headers=[("Content-Length", "5")])
            else:
                self.reply(handler)

        self.origin.reply = reply
        responses = self.simultaneous_misses(3)
        self.assertCountEqual([status for status, _, _ in responses], [502, 200, 200])
        self.assertEqual(len(self.origin.requests), 2)
        self.assertEqual(self.request()[1]["X-Cache"], "HIT")
        self.assertFalse(proxy._key_locks)

    def test_noncacheable_result_is_never_shared(self):
        self.origin.reply = lambda h: self.reply(h, headers=[
            ("Content-Length", "5"), ("Cache-Control", "private")])
        responses = self.simultaneous_misses(3)
        self.assertEqual(len(self.origin.requests), 3)
        self.assertTrue(all(h["X-Cache"] == "MISS" for _, h, _ in responses))
        self.assertFalse(self.cache.entries)
        self.assertFalse(proxy._key_locks)

    def test_redis_read_failure_bypasses_cache_then_recovers(self):
        # Exercise failure on the initial lookup and on the locked recheck.
        for sequence in [[RedisConnectionError()], [None, RedisTimeoutError()]]:
            with self.subTest(sequence=sequence):
                self.cache.entries.clear()
                with patch.object(self.cache, "get", side_effect=sequence):
                    status, headers, body = self.request()
                    self.assertEqual((status, headers["X-Cache"], body), (200, "MISS", b"hello"))
                self.assertFalse(self.cache.entries)
                self.assertFalse(proxy._key_locks)
                self.assertEqual(self.request()[1]["X-Cache"], "MISS")
                self.assertEqual(self.request()[1]["X-Cache"], "HIT")

    def test_redis_write_failure_returns_origin_body_and_releases_waiters(self):
        with patch.object(self.cache, "set", side_effect=RedisConnectionError()):
            responses = self.simultaneous_misses(3)
        for status, headers, body in responses:
            self.assertEqual((status, headers["X-Cache"], body), (200, "MISS", b"hello"))
        self.assertEqual(len(self.origin.requests), 3)
        self.assertFalse(proxy._key_locks)
        self.assertEqual(self.request()[1]["X-Cache"], "MISS")
        self.assertEqual(self.request()[1]["X-Cache"], "HIT")

    def test_unexpected_exception_releases_key(self):
        with self.assertRaises(RuntimeError):
            with proxy.lock_for_key("example"):
                raise RuntimeError("failed fetch")
        self.assertFalse(proxy._key_locks)

    def test_failed_clear_has_nonzero_exit(self):
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["proxy", "--clear-cache"]), contextlib.redirect_stderr(stderr):
            with patch.object(cli.cache, "clear", side_effect=RedisConnectionError("offline")):
                with self.assertRaises(SystemExit) as result:
                    cli.main()
        self.assertEqual(result.exception.code, 1)
        self.assertIn("Cache clear failed", stderr.getvalue())
