
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

from proxy_server.cache import cache
import proxy_server.config


EXCLUDED_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
}


class ProxyRequestHandler(BaseHTTPRequestHandler):

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
        if not 200 <= status_code < 400:
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

        # Vary: * cannot be reused through ordinary cache matching.
        vary_values = response_headers.get("vary", [])

        for value in vary_values:
            if "*" in [item.strip() for item in value.split(",")]:
                return False

        return True

    def handle_request(self):
        cache_key = f"{self.command}:{self.path}"

        # 1. Decide whether this request can use the cache.
        can_cache_request = (
            self.command == "GET"
            and self.can_use_cache(self.headers)
        )

        cached_response = None

        # 2. Look for a cached response only when permitted.
        if can_cache_request:
            cached_response = cache.get(cache_key)

        if cached_response:
            self.send_response(cached_response["status_code"])

            for key, value in cached_response["headers"]:
                if key.lower() not in EXCLUDED_HEADERS:
                    self.send_header(key, value)

            self.send_header("X-Cache", "HIT")
            self.end_headers()
            self.wfile.write(cached_response["body"])
            return

        # 3. Cache miss or cache bypass: forward to the origin.
        target_url = (
            f"{proxy_server.config.ORIGIN_URL.rstrip('/')}/"
            f"{self.path.lstrip('/')}"
        )

        content_length = int(self.headers.get("Content-Length", 0))
        req_body = (
            self.rfile.read(content_length)
            if content_length > 0
            else None
        )

        req = urllib.request.Request(
            url=target_url,
            data=req_body,
            method=self.command,
        )

        for key, value in self.headers.items():
            if key.lower() not in EXCLUDED_HEADERS:
                req.add_header(key, value)

        try:
            with urllib.request.urlopen(req) as response:
                status_code = response.status
                headers = list(response.getheaders())
                res_body = response.read()

        except urllib.error.HTTPError as error:
            status_code = error.code
            headers = list(error.headers.items())
            res_body = error.read()

        except Exception as error:
            self.send_error(500, f"Proxy Error: {str(error)}")
            return

        # 4. Store only eligible GET responses.
        if (
            can_cache_request
            and self.can_store_response(status_code, headers)
        ):
            cache.set(cache_key, status_code, headers, res_body)

        # 5. Return the origin response to the client.
        self.send_response(status_code)

        for key, value in headers:
            if key.lower() not in EXCLUDED_HEADERS:
                self.send_header(key, value)

        self.send_header("X-Cache", "MISS")
        self.end_headers()
        self.wfile.write(res_body)

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
