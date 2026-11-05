import argparse
import sys
import math
from redis.exceptions import RedisError
from http.server import ThreadingHTTPServer
import proxy_server.config
import proxy_server.proxy
from proxy_server.cache import cache

def positive_timeout(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Timeout must be finite and greater than zero")
    return number


def positive_size(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Size must be greater than zero")
    return number


def main():
    parser = argparse.ArgumentParser(description="Caching Proxy CLI Tool")
    parser.add_argument("--port", type=int, help="Port on which the proxy server will run")
    parser.add_argument("--origin", type=str, help="Origin URL to forward requests to")
    parser.add_argument("--clear-cache", action="store_true", help="Clear the stored cache")
    parser.add_argument("--origin-timeout", type=positive_timeout, default=10.0,
                        help="Origin socket timeout in seconds (default: 10)")
    parser.add_argument("--client-timeout", type=positive_timeout, default=10.0,
                        help="Client socket timeout in seconds (default: 10)")
    parser.add_argument("--max-request-body", type=positive_size, default=1024 * 1024,
                        help="Maximum request body in bytes (default: 1048576)")
    parser.add_argument("--max-cache-body", type=positive_size, default=1024 * 1024,
                        help="Maximum buffered/cacheable response in bytes (default: 1048576)")

    args = parser.parse_args()

    if args.clear_cache:
        try:
            cache.clear()
        except RedisError as error:
            parser.exit(1, f"Cache clear failed: {error}\n")
        sys.exit(0)

    if not args.port or not args.origin:
        parser.error("Both --port and --origin are required unless --clear-cache is specified.")

    proxy_server.config.ORIGIN_URL = args.origin
    proxy_server.config.ORIGIN_TIMEOUT = args.origin_timeout
    proxy_server.config.CLIENT_TIMEOUT = args.client_timeout
    proxy_server.config.MAX_REQUEST_BODY = args.max_request_body
    proxy_server.config.MAX_CACHE_BODY = args.max_cache_body

    print(f"Starting proxy server on port {args.port}")
    print(f"Forwarding requests to {args.origin}")

    server_address = ("127.0.0.1", args.port)
    httpd = ThreadingHTTPServer(server_address, proxy_server.proxy.ProxyRequestHandler)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping proxy server.")
        httpd.server_close()

if __name__ == "__main__":
    main()
