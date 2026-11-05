
import json
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from proxy_server.config import CACHE_TTL

REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
CACHE_PREFIX = "caching-proxy:"


class CacheStore:
    def __init__(self):
        self._redis = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=False,
            socket_connect_timeout=1,
            socket_timeout=1,
            retry=Retry(NoBackoff(), 0),
        )

    def get(self, key):
        cache_key = CACHE_PREFIX + key
        cached_data = self._redis.get(cache_key)

        if cached_data is None:
            return None

        data = json.loads(cached_data)

        return {
            "status_code": data["status_code"],
            "headers": data["headers"],
            "body": bytes.fromhex(data["body"]),
        }

    def set(self, key, status_code, headers, body):
        cache_key = CACHE_PREFIX + key

        data = {
            "status_code": status_code,
            "headers": list(headers),
            "body": body.hex(),
        }

        self._redis.set(
            cache_key,
            json.dumps(data),
            ex=CACHE_TTL,
        )

    def clear(self):
        pattern = CACHE_PREFIX + "*"

        for key in self._redis.scan_iter(match=pattern):
            self._redis.delete(key)


cache = CacheStore()
