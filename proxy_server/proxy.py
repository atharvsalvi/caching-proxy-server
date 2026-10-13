import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler
from proxy_server.cache import cache
import proxy_server.config

EXCLUDED_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}

class ProxyRequestHandler(BaseHTTPRequestHandler):

    def handle_request(self):
        cache_key = f"{self.command}:{self.path}"
        
        # 1. Check Cache
        cached_response = cache.get(cache_key)
        if cached_response:
            self.send_response(cached_response["status_code"])
            for key, val in cached_response["headers"]:
                if key.lower() not in EXCLUDED_HEADERS:
                    self.send_header(key, val)
            self.send_header("X-Cache", "HIT")
            self.end_headers()
            self.wfile.write(cached_response["body"])
            return

        # 2. Cache Miss: Forward Request to Origin Server
        target_url = f"{proxy_server.config.ORIGIN_URL.rstrip('/')}/{self.path.lstrip('/')}"
        
        # Read incoming request body if present
        content_length = int(self.headers.get("Content-Length", 0))
        req_body = self.rfile.read(content_length) if content_length > 0 else None

        # Build forwarding request
        req = urllib.request.Request(
            url=target_url,
            data=req_body,
            method=self.command
        )

        # Copy client headers
        for key, val in self.headers.items():
            if key.lower() not in EXCLUDED_HEADERS:
                req.add_header(key, val)

        try:
            with urllib.request.urlopen(req) as response:
                status_code = response.status
                headers = response.getheaders()
                res_body = response.read()
        except urllib.error.HTTPError as e:
            status_code = e.code
            headers = e.headers.items()
            res_body = e.read()
        except Exception as e:
            self.send_error(500, f"Proxy Error: {str(e)}")
            return

        # 3. Store in Cache if successful
        if status_code < 400:
            cache.set(cache_key, status_code, headers, res_body)

        # 4. Respond to client
        self.send_response(status_code)
        for key, val in headers:
            if key.lower() not in EXCLUDED_HEADERS:
                self.send_header(key, val)
        self.send_header("X-Cache", "MISS")
        self.end_headers()
        self.wfile.write(res_body)

    # Route all standard HTTP methods to the same request handler
    def do_GET(self): self.handle_request()
    def do_POST(self): self.handle_request()
    def do_PUT(self): self.handle_request()
    def do_DELETE(self): self.handle_request()
    def do_PATCH(self): self.handle_request()

    # Suppress default http.server logging to keep terminal output clean
    def log_message(self, format, *args):
        return