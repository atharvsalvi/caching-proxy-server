"""Use a controlled clock for freshness and real local HTTP for validation."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from email.utils import formatdate
from unittest.mock import patch

import test_http_handling as http_tests
import test_concurrency as concurrency_tests
from proxy_server import config, policy, proxy
from proxy_server.cache import CacheStore


class PolicyTests(unittest.TestCase):
    def test_grace_boundaries_and_restrictions(self):
        entry = dict(received_at=100, initial_age=2, lifetime=5)
        rules = "max-age=5, stale-while-revalidate=10"
        with patch.object(policy, "time", return_value=103):
            for rule in ["must-revalidate", "proxy-revalidate", "no-cache", "s-maxage=5",
                         "stale-while-revalidate=100"]:
                entry["headers"] = [("Cache-Control", rules + ", " + rule)]
                self.assertFalse(policy.can_serve_stale(entry, {}))
            entry["headers"] = [("Cache-Control", rules)]
            self.assertTrue(policy.can_serve_stale(entry, {}))
            self.assertFalse(policy.can_serve_stale(entry, {"max-age": "0"}))
            self.assertFalse(policy.can_serve_stale(entry, {"min-fresh": "1"}))
        with patch.object(policy, "time", return_value=113):
            self.assertFalse(policy.can_serve_stale(entry, {}))

    def test_precedence_dates_and_age(self):
        date = formatdate(1000, usegmt=True)
        fields = [("Date", date), ("Age", "8"), ("Expires", formatdate(1050, usegmt=True))]
        for extra, lifetime in [
            ([], 50), ([("Cache-Control", "max-age=30")], 30),
            ([("Cache-Control", 'MAX-AGE="30", s-maxage=10')], 10),
            ([("Cache-Control", "s-maxage=bad, max-age=20")], 0),
            ([("Cache-Control", "max-age=10"), ("cache-control", "max-age=20")], 0),
            ([("Cache-Control", 'ext="x, private", max-age=30')], 30),
        ]:
            with self.subTest(extra=extra):
                timing = policy.freshness(fields + extra, 1002, 1005)
                self.assertEqual(timing["lifetime"], lifetime)
                self.assertEqual(policy.age(timing, 1006), 12)
        self.assertEqual(policy.freshness([], 1000, 1000)["lifetime"], 60)
        self.assertEqual(policy.freshness([("Expires", "bad")], 1000, 1000)["lifetime"], 0)
        self.assertEqual(policy.freshness([("Date", date)], 1100, 1101)["initial_age"], 101)

    def test_redis_payload_and_retention_are_distinct_from_freshness(self):
        cache = CacheStore()
        timing = dict(initial_age=2, received_at=1000, lifetime=10)
        with patch.object(cache, "_redis") as redis, patch.object(policy, "time", return_value=1003):
            for headers, ttl in [([], 5), ([("ETag", '"v1"')], 305),
                                 ([("Cache-Control", "stale-while-revalidate=10")], 15)]:
                cache.set("key", 200, headers, b"bytes", timing)
                key, payload = redis.set.call_args.args
                self.assertEqual(redis.set.call_args.kwargs["ex"], ttl)
                self.assertEqual(json.loads(payload)["lifetime"], 10)
                redis.get.return_value = payload
                self.assertEqual(cache.get("key")["body"], b"bytes")
                self.assertFalse(policy.fresh(cache.get("key"), {"max-age": "0"}))


class FreshnessHTTPTests(unittest.TestCase):
    start_server = http_tests.HTTPHandlingTests.start_server
    request = http_tests.HTTPHandlingTests.request

    def setUp(self):
        http_tests.HTTPHandlingTests.setUp(self)
        self.now = 1700000000
        self.enterContext(patch.object(policy, "time", side_effect=lambda: self.now))
        self.body = b"one"
        self.tag = '"one"'
        self.modified = formatdate(self.now - 60, usegmt=True)
        self.rules = "max-age=5, must-revalidate"
        self.extra = []
        self.status = 200
        self.tag_on_304 = None
        self.origin.reply = self.reply_resource

    @staticmethod
    def reply(handler, *args, **kwargs):
        http_tests.HTTPHandlingTests.reply(handler, *args, **kwargs)

    def reply_resource(self, handler):
        tag = handler.headers.get("If-None-Match")
        modified = handler.headers.get("If-Modified-Since")
        matches = tag == self.tag if tag is not None else bool(modified and modified == self.modified)
        status = 304 if matches and self.status == 200 else self.status
        handler.send_response_only(status)
        handler.send_header("Date", formatdate(self.now, usegmt=True))
        if self.rules is not None:
            handler.send_header("Cache-Control", self.rules)
        if self.tag:
            handler.send_header("ETag", self.tag_on_304 if status == 304 and self.tag_on_304 else self.tag)
        if self.modified:
            handler.send_header("Last-Modified", self.modified)
        for name, value in self.extra:
            handler.send_header(name, value)
        if status != 304:
            handler.send_header("Content-Length", str(len(self.body)))
        handler.end_headers()
        if status != 304:
            handler.wfile.write(self.body)

    def check(self, cache_status, body=b"one", headers=None):
        status, fields, result = self.request(headers=headers)
        self.assertEqual((status, fields["X-Cache"], result), (200, cache_status, body))
        return fields

    def test_expiry_304_updates_metadata_and_returns_body(self):
        self.check("MISS")
        self.now += 4
        self.assertEqual(self.check("HIT")["Age"], "4")
        self.now += 1
        self.rules = "max-age=30"
        self.extra = [("X-Version-Checked", "yes")]
        fields = self.check("REVALIDATED")
        self.assertEqual(fields["X-Version-Checked"], "yes")
        self.assertEqual(fields["Age"], "0")
        self.assertEqual(self.origin.request_headers[-1]["If-None-Match"], '"one"')
        self.now += 20
        self.check("HIT")
        self.assertEqual(len(self.origin.requests), 2)

    def test_changed_etag_replaces_body(self):
        self.check("MISS")
        self.now += 5
        self.tag, self.body = '"two"', b"two"
        self.check("MISS", b"two")
        self.check("HIT", b"two")

    def wait_for_refresh(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with proxy._registry_lock:
                if not proxy._refreshing:
                    return
            time.sleep(0.005)
        self.fail("refresh still active")

    def test_four_clients_finish_while_changed_body_is_still_refreshing(self):
        self.rules = "max-age=5, stale-while-revalidate=10"
        self.check("MISS")
        self.now += 5
        self.tag, self.body = '"two"', b"two"
        release, entered = threading.Event(), threading.Event()
        def delayed(handler):
            entered.set()
            release.wait(4)
            self.reply_resource(handler)
        self.origin.reply = delayed
        with patch.object(config, "ORIGIN_TIMEOUT", 5):
            try:
                with ThreadPoolExecutor(4) as pool:
                    clients = [pool.submit(self.request) for _ in range(4)]
                    self.assertTrue(entered.wait(2))
                    for client in clients:
                        status, fields, body = client.result(timeout=2)
                        self.assertEqual((status, fields["X-Cache"], body), (200, "STALE", b"one"))
                        self.assertEqual(fields["Age"], "5")
                self.assertEqual(len(self.origin.requests), 2)
            finally:
                release.set()
                self.wait_for_refresh()
        self.check("HIT", b"two")
        self.assertEqual(len(self.origin.requests), 2)

    def test_refresh_failure_does_not_extend_grace(self):
        self.rules = "max-age=5, stale-while-revalidate=10"
        self.check("MISS")
        self.now += 14
        self.status, self.body = 503, b"offline"
        self.check("STALE")
        self.wait_for_refresh()
        self.now += 1
        self.assertEqual(self.request()[0], 503)

    def test_background_full_fetch_without_validator(self):
        self.tag = self.modified = None
        self.rules = "max-age=5, stale-while-revalidate=10"
        self.check("MISS")
        self.now += 5
        self.body = b"two"
        self.check("STALE")
        self.wait_for_refresh()
        self.check("HIT", b"two")

    def test_worker_limit_falls_back_to_blocking_validation(self):
        self.rules = "max-age=5, stale-while-revalidate=10"
        self.check("MISS")
        self.now += 5
        with patch.object(proxy, "MAX_BACKGROUND_REFRESHES", 0):
            self.check("REVALIDATED")

    def test_concurrent_expired_requests_validate_once(self):
        self.check("MISS")
        self.now += 5
        responses = concurrency_tests.ConcurrencyTests.simultaneous_misses(self, 4)
        self.assertCountEqual([h["X-Cache"] for _, h, _ in responses],
                              ["REVALIDATED", "HIT", "HIT", "HIT"])
        self.assertEqual(len(self.origin.requests), 2)  # First fill plus one validation.

    def test_malformed_etag_is_never_sent_as_a_condition(self):
        self.tag, self.modified = "*", None
        self.check("MISS")
        self.now += 5
        self.body = b"new"
        self.check("MISS", b"new")
        self.assertIsNone(self.origin.request_headers[-1].get("If-None-Match"))

    def test_no_cache_validates_every_use_with_last_modified(self):
        self.tag = None
        self.rules = "no-cache, max-age=600"
        self.check("MISS")
        for _ in range(2):
            self.check("REVALIDATED")
            self.assertEqual(self.origin.request_headers[-1]["If-Modified-Since"], self.modified)
        self.assertEqual(len(self.origin.requests), 3)

    def test_new_private_policy_drops_saved_entry(self):
        self.check("MISS")
        self.now += 5
        self.rules = "private"
        self.check("MISS")
        self.assertFalse(self.cache.entries)
        self.assertIsNone(self.origin.request_headers[-1].get("If-None-Match"))
        self.check("MISS")

    def test_unsupported_new_vary_and_mismatched_tag_retry_unconditionally(self):
        for extra, tag in [([("Vary", "X-Custom")], None), ([], '"other"')]:
            with self.subTest(extra=extra, tag=tag):
                self.cache.entries.clear()
                self.extra, self.tag_on_304 = [], None
                self.check("MISS")
                self.now += 5
                self.extra, self.tag_on_304 = extra, tag
                self.check("MISS")
                self.assertIsNone(self.origin.request_headers[-1].get("If-None-Match"))

    def test_expires_without_validator_fetches_new_body(self):
        self.tag = self.modified = self.rules = None
        self.extra = [("Expires", formatdate(self.now + 10, usegmt=True))]
        self.check("MISS")
        self.now += 9
        self.check("HIT")
        self.now += 1
        self.body = b"two"
        self.check("MISS", b"two")
        self.assertIsNone(self.origin.request_headers[-1].get("If-Modified-Since"))

    def test_stale_origin_error_never_serves_old_body(self):
        self.check("MISS")
        self.now += 5
        self.status, self.body = 503, b"unavailable"
        status, _, body = self.request()
        self.assertEqual((status, body), (503, b"unavailable"))

    def test_s_maxage_and_upstream_age_limit_hit_window(self):
        self.rules = "s-maxage=10, max-age=3600"
        self.extra = [("Age", "8")]
        self.check("MISS")
        self.now += 1
        self.assertEqual(self.check("HIT")["Age"], "9")
        self.now += 1
        self.check("REVALIDATED")

    def test_request_max_age_and_min_fresh(self):
        self.check("MISS")
        self.now += 2
        self.check("HIT", headers={"Cache-Control": "max-age=4"})
        self.check("REVALIDATED", headers={"Cache-Control": "max-age=0"})
        self.check("REVALIDATED", headers={"Cache-Control": "min-fresh=10"})

    def test_no_store_private_and_no_cache_without_validator(self):
        for rules in ["no-store, max-age=600", 'private="Example"', "no-cache"]:
            with self.subTest(rules=rules):
                self.tag = self.modified = None
                self.rules = rules
                self.check("MISS")
                self.check("MISS")
                self.assertFalse(self.cache.entries)
