
import json
import math
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from proxy_server import config, policy

REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
CACHE_PREFIX = "caching-proxy:"


def unpack_entry(cached_data):
    if cached_data is None:
        return None

    data = json.loads(cached_data)

    data["body"] = bytes.fromhex(data["body"])
    return data


def pack_entry(status_code, headers, body, timing):
    data = {
        **timing,
        "status_code": status_code,
        "headers": list(headers),
        "body": body.hex(),
    }

    remaining = timing["lifetime"] - policy.age(timing, policy.time())
    retention = config.VALIDATOR_RETENTION if policy.validators(headers) else 0
    ttl = max(1, math.ceil(max(max(0, remaining) + retention,
                               remaining + policy.grace_seconds(headers))))
    return json.dumps(data), ttl


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

        return unpack_entry(cached_data)

    def set(self, key, status_code, headers, body, timing):
        cache_key = CACHE_PREFIX + key

        payload, ttl = pack_entry(status_code, headers, body, timing)
        self._redis.set(
            cache_key,
            payload,
            ex=ttl,
        )

    def delete(self, key):
        self._redis.delete(CACHE_PREFIX + key)

    def clear(self):
        pattern = CACHE_PREFIX + "*"

        for key in self._redis.scan_iter(match=pattern):
            self._redis.delete(key)


cache = CacheStore()
