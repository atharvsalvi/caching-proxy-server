import os
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError, TimeoutError

from proxy_server import cache as cache_module


def pytest_addoption(parser):
    parser.addoption("--require-redis", action="store_true",
                     help="Fail live tests if the local Redis server is unavailable")


@pytest.fixture
def live_store(request, monkeypatch):
    # Keep test keys outside the production clear-cache prefix.
    monkeypatch.setattr(cache_module, "CACHE_PREFIX", f"phase5-test:{uuid4().hex}:")
    port = int(os.environ.get("PROXY_TEST_REDIS_PORT", "6379"))
    monkeypatch.setattr(cache_module, "REDIS_PORT", port)
    store = cache_module.CacheStore()
    try:
        try:
            store._redis.ping()
        except (ConnectionError, TimeoutError) as error:
            message = f"Redis unavailable at 127.0.0.1:{port}: {error}"
            if request.config.getoption("--require-redis"):
                pytest.fail(message)
            pytest.skip(message)
        try:
            yield store
        finally:
            store.clear()
    finally:
        store._redis.close()
