
import json
import http.client
import threading
import urllib.request
import urllib.error
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler

from redis.exceptions import RedisError
from proxy_server.cache import cache
import proxy_server.config


EXCLUDED_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "upgrade", "proxy-connection",
}

# Always key these fields, so no separate Redis variant index is needed.
SUPPORTED_VARY_HEADERS = ("accept", "accept-language", "accept-encoding")

# Track holders and waiters so a key keeps the same lock until its last user exits.
_key_locks = {}
_registry_lock = threading.Lock()


@contextmanager
def lock_for_key(key):
    with _registry_lock:
        lock, users = _key_locks.get(key, (None, 0))
        if lock is None:
            lock = threading.Lock()
        _key_locks[key] = (lock, users + 1)
    try:
        with lock:
            yield
    finally:
        with _registry_lock:
            _, users = _key_locks[key]
            if users == 1:
                del _key_locks[key]
            else:
                _key_locks[key] = (lock, users - 1)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def excluded_headers(headers):
    excluded = set(EXCLUDED_HEADERS)
    for name, value in headers:
        if name.lower() == "connection":
            excluded.update(item.strip().lower() for item in value.split(","))
    return excluded


def content_length(headers):
    """Reject ambiguous framing, including duplicate Content-Length fields."""
    values = headers.get_all("Content-Length", [])
    if not values:
        return None
    value = values[0].strip()
    if len(values) != 1 or not value or any(char not in "0123456789" for char in value):
        raise ValueError("Invalid or duplicate Content-Length")
    return int(value)


class ProxyRequestHandler(BaseHTTPRequestHandler):

    def setup(self):
        super().setup()
        self.connection.settimeout(proxy_server.config.CLIENT_TIMEOUT)

    @staticmethod
    def build_cache_key(method, target_url, forwarded_headers):
        """Identify the resource and the supported representation preferences."""
        variants = [[name, forwarded_headers.get(name)] for name in SUPPORTED_VARY_HEADERS]
        # v4 also avoids old entries created with automatic redirect following
        # and the earlier forwarding/framing rules.
        return "v4:" + json.dumps([method, target_url, variants], separators=(",", ":"))

    @staticmethod
    def build_forwarded_headers(headers):
        """Use the same header values for cache matching and origin requests."""
        excluded = excluded_headers(headers.items())
        forwarded = {
            name.lower(): value for name, value in headers.items()
            if name.lower() not in excluded
        }
        # These three fields accept comma-separated lists. Preserve all values
        # and their order instead of silently keeping only the last field line.
        for name in SUPPORTED_VARY_HEADERS:
            if name in excluded:
                continue
            values = headers.get_all(name)
            if values is not None:
                forwarded[name] = ", ".join(values)
        # urllib/http.client sends identity when Accept-Encoding is absent.
        # Make that default explicit before building the cache key.
        forwarded.setdefault("accept-encoding", "identity")
        return forwarded

    @staticmethod
    def has_directive(headers, directive):
        """Check whether Cache-Control contains a directive."""
        cache_control = headers.get("Cache-Control", "")

        directives = {
            item.strip().split("=", 1)[0].strip().lower()
            for item in cache_control.split(",")
        }

        return directive.lower() in directives

    @staticmethod
    def can_use_cache(headers):
        """Determine whether the request may use the shared cache."""
        if ProxyRequestHandler.has_directive(headers, "no-store"):
            return False

        if ProxyRequestHandler.has_directive(headers, "no-cache"):
            return False

        if headers.get("Authorization") or headers.get("Cookie"):
            return False

        return True

    @staticmethod
    def can_store_response(status_code, headers):
        """Determine whether the origin response may be cached."""
        if not 200 <= status_code < 400 or status_code in (206, 304):
            return False

        # Read response headers case-insensitively.
        response_headers = {}

        for key, value in headers:
            response_headers.setdefault(key.lower(), []).append(value)

        cache_control = ", ".join(
            response_headers.get("cache-control", [])
        ).lower()

        directives = {
            item.strip().split("=", 1)[0].strip()
            for item in cache_control.split(",")
        }

        # Conservative policy: do not store these responses.
        if directives.intersection({"no-store", "no-cache", "private"}):
            return False

        # Avoid caching responses that set client-specific cookies.
        if "set-cookie" in response_headers:
            return False

        # Only store Vary responses when every named field is in our key.
        # This also rejects Vary: * and unknown or malformed field names.
        vary_values = response_headers.get("vary", [])

        for value in vary_values:
            for name in value.split(","):
                name = name.strip().lower()
                if name and name not in SUPPORTED_VARY_HEADERS:
                    return False

        return True

    def send_origin_headers(self, status, headers, length, cache_status):
        self.send_response(status)
        excluded = excluded_headers(headers) | {"x-cache"}
        for name, value in headers:
            if name.lower() not in excluded:
                self.send_header(name, value)
        if length is not None and status not in (204, 304):
            self.send_header("Content-Length", str(length))
        self.send_header("X-Cache", cache_status)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def handle_request(self):
        # Validate framing before a cache hit can skip request processing.
        self.close_connection = True
        try:
            length = content_length(self.headers)
        except ValueError:
            self.send_error(400, "Invalid or duplicate Content-Length")
            return
        if "Transfer-Encoding" in self.headers:
            self.send_error(400 if length is not None else 501,
                            "Request Transfer-Encoding is not supported")
            return
        if "Expect" in self.headers:
            self.send_error(417, "Expect is not supported")
            return
        if (length or 0) > proxy_server.config.MAX_REQUEST_BODY:
            self.send_error(413, "Request body exceeds configured limit")
            return
        try:
            req_body = self.rfile.read(length) if length else None
        except TimeoutError:
            self.send_error(408, "Request body timed out")
            return
        except OSError:
            return
        if length and len(req_body) != length:
            self.send_error(400, "Incomplete request body")
            return
        # Use the same complete URL for cache identity and origin forwarding.
        # Preserve the query string, including ordering and repeated parameters.
        target_url = (
            f"{proxy_server.config.ORIGIN_URL.rstrip('/')}/"
            f"{self.path.lstrip('/')}"
        )
        forwarded_headers = self.build_forwarded_headers(self.headers)
        cache_key = self.build_cache_key(self.command, target_url, forwarded_headers)

        # 1. Decide whether this request can use the cache.
        can_cache_request = (
            self.command == "GET"
            and not length
            and "Range" not in self.headers
            and not any(name.lower().startswith("if-") for name in self.headers)
            and self.can_use_cache(self.headers)
        )

        if not can_cache_request:
            self.forward_request(target_url, forwarded_headers, req_body, None)
            return

        try:
            cached_response = cache.get(cache_key)
        except RedisError:
            self.forward_request(target_url, forwarded_headers, req_body, None)
            return
        if cached_response is None:
            with lock_for_key(cache_key):
                # Another thread might have populated Redis since our first lookup.
                try:
                    cached_response = cache.get(cache_key)
                except RedisError:
                    cache_key = None
                if cached_response is None and cache_key is not None:
                    self.forward_request(target_url, forwarded_headers, req_body, cache_key)
                    return
            if cache_key is None:
                self.forward_request(target_url, forwarded_headers, req_body, None)
                return

        try:
            self.send_origin_headers(cached_response["status_code"],
                                     cached_response["headers"], len(cached_response["body"]), "HIT")
            self.wfile.write(cached_response["body"])
        except OSError:
            pass  # The client disconnected or stopped reading.

    def forward_request(self, target_url, forwarded_headers, req_body, cache_key):
        req = urllib.request.Request(
            url=target_url,
            data=req_body,
            method=self.command,
        )

        for key, value in forwarded_headers.items():
            req.add_header(key, value)

        # Connect directly to the configured origin; return redirects unchanged.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        response_started = False
        try:
            try:
                response = opener.open(req, timeout=proxy_server.config.ORIGIN_TIMEOUT)
            except urllib.error.HTTPError as error:
                response = error  # HTTP errors and redirects still have responses to forward.
            with response:
                status_code = response.status
                headers = list(response.headers.items())
                response_length = content_length(response.headers)
                encodings = response.headers.get_all("Transfer-Encoding", [])
                if encodings and (response_length is not None or
                                  ",".join(encodings).strip().lower() != "chunked"):
                    raise ValueError("Unsupported or ambiguous origin framing")
                if status_code in (204, 304):
                    response_length = 0
                if not 200 <= status_code < 600:
                    raise ValueError("Unsupported origin status")
                store_response = (
                    cache_key is not None and self.can_store_response(status_code, headers)
                    and response_length is not None
                    and response_length <= proxy_server.config.MAX_CACHE_BODY
                )
                if store_response:
                    body = response.read(response_length)
                    if len(body) != response_length:
                        raise ValueError("Incomplete origin response")
                    try:
                        cache.set(cache_key, status_code, headers, body)
                    except RedisError:
                        pass  # The origin response is still valid for this client.
                    response_started = True
                    self.send_origin_headers(status_code, headers, len(body), "MISS")
                    self.wfile.write(body)
                else:
                    # Unknown-length and large bodies never accumulate in memory.
                    response_started = True
                    self.send_origin_headers(status_code, headers, response_length, "MISS")
                    if status_code not in (204, 304):
                        while True:
                            chunk = response.read1(proxy_server.config.STREAM_CHUNK_SIZE)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
        except (OSError, http.client.HTTPException, ValueError) as error:
            # After streaming starts, another status line would corrupt the body.
            # Closing the connection is the only available failure signal.
            if not response_started:
                reason = getattr(error, "reason", error)
                self.send_error(504 if isinstance(reason, TimeoutError) else 502,
                                "Origin timed out" if isinstance(reason, TimeoutError)
                                else "Invalid or unavailable origin response")

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PUT(self):
        self.handle_request()

    def do_DELETE(self):
        self.handle_request()

    # Suppress default http.server logging.
    def log_message(self, format, *args):
        return
