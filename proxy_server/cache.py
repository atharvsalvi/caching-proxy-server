import threading
from typing import Optional, Dict, Any

class CacheStore:
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            if key in self._cache:
                print(f"Cache : HIT {key}")
                return self._cache[key]
            else:
                print(f"Cache Not Present for key : {key}")
                print(f"Cache : MISS {key}")
                return None

    def set(self, key: str, status_code: int, headers: list, body: bytes) -> None:
        with self._lock:
            self._cache[key] = {
                "status_code": status_code,
                "headers": headers,
                "body": body
            }

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            print("Cache cleared successfully.")

# Global cache instance
cache = CacheStore()