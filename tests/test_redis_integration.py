import http.client
import socket
import sys
import time
from uuid import uuid4

import pytest

from proxy_server import cache as cache_module, cli, config, proxy
from server_helpers import running_server
from test_http_handling import Origin


@pytest.fixture
def local_proxy(monkeypatch):
    with running_server(Origin) as origin:
        monkeypatch.setattr(config, "ORIGIN_URL", f"http://127.0.0.1:{origin.server_port}")
        with running_server(proxy.ProxyRequestHandler) as server:
            def get(path="/item"):
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    client.request("GET", path)
                    response = client.getresponse()
                    return response.status, response.getheader("X-Cache"), response.read()
                finally:
                    client.close()

            yield origin, get


def respond(handler, cache_control="max-age=60", tag=None):
    body = b"\x00\xfforigin body"
    unchanged = tag and tag == handler.headers.get("If-None-Match")
    handler.send_response(304 if unchanged else 200)
    handler.send_header("Cache-Control", cache_control)
    if tag:
        handler.send_header("ETag", tag)
    if not unchanged:
        handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if not unchanged:
        handler.wfile.write(body)


@pytest.mark.redis
def test_cli_clear_removes_only_test_entries(live_store, local_proxy, monkeypatch):
    origin, get = local_proxy
    origin.reply = respond
    monkeypatch.setattr(proxy, "cache", live_store)
    monkeypatch.setattr(cli, "cache", live_store)
    # This key belongs to a different namespace and has its own short expiry.
    sentinel = f"phase5-other:{uuid4().hex}"
    live_store._redis.set(sentinel, "keep", ex=60)
    try:
        first = get()
        assert first == (200, "MISS", b"\x00\xfforigin body")
        assert get() == (200, "HIT", first[2])
        assert get("/other")[1] == "MISS"
        assert len(origin.requests) == 2
        monkeypatch.setattr(sys, "argv", ["caching-proxy", "--clear-cache"])
        with pytest.raises(SystemExit) as result:
            cli.main()
        assert result.value.code == 0
        assert list(live_store._redis.scan_iter(match=cache_module.CACHE_PREFIX + "*")) == []
        assert live_store._redis.get(sentinel) == b"keep"
        assert get() == first
        assert len(origin.requests) == 3
    finally:
        live_store._redis.delete(sentinel)


@pytest.mark.redis
def test_redis_physically_expires_unvalidated_entry(live_store, local_proxy, monkeypatch):
    origin, get = local_proxy
    origin.reply = lambda h: respond(h, "max-age=2")
    monkeypatch.setattr(proxy, "cache", live_store)
    assert get()[1] == "MISS"
    key, = live_store._redis.scan_iter(match=cache_module.CACHE_PREFIX + "*")
    assert 0 < live_store._redis.pttl(key) <= 2000
    deadline = time.monotonic() + 5
    while live_store._redis.exists(key) and time.monotonic() < deadline:
        time.sleep(0.03)
    assert not live_store._redis.exists(key)
    assert get()[1] == "MISS"
    assert len(origin.requests) == 2


@pytest.mark.redis
def test_stale_validator_survives_and_revalidates(live_store, local_proxy, monkeypatch):
    origin, get = local_proxy
    origin.reply = lambda h: respond(h, "max-age=0", '"v1"')
    monkeypatch.setattr(proxy, "cache", live_store)
    status, label, body = get()
    assert (status, label) == (200, "MISS")
    key, = live_store._redis.scan_iter(match=cache_module.CACHE_PREFIX + "*")
    assert 0 < live_store._redis.ttl(key) <= config.VALIDATOR_RETENTION
    assert get() == (200, "REVALIDATED", body)
    assert origin.request_headers[-1]["If-None-Match"] == '"v1"'
    assert len(origin.requests) == 2


def test_unreachable_redis_serves_the_origin(local_proxy, monkeypatch):
    origin, get = local_proxy
    origin.reply = respond
    # Reserve a port without listening. The Redis client experiences a real
    # connection failure without stopping a running Redis server.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        monkeypatch.setattr(cache_module, "REDIS_PORT", reserved.getsockname()[1])
        store = cache_module.CacheStore()
        monkeypatch.setattr(proxy, "cache", store)
        try:
            for _ in range(2):
                assert get() == (200, "MISS", b"\x00\xfforigin body")
        finally:
            store._redis.close()
    assert len(origin.requests) == 2
