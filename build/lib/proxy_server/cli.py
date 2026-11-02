import argparse
import sys
from http.server import ThreadingHTTPServer
import proxy_server.config
import proxy_server.proxy
from proxy_server.cache import cache

def main():
    parser = argparse.ArgumentParser(description="Caching Proxy CLI Tool")
    parser.add_argument("--port", type=int, help="Port on which the proxy server will run")
    parser.add_argument("--origin", type=str, help="Origin URL to forward requests to")
    parser.add_argument("--clear-cache", action="store_true", help="Clear the stored cache")

    args = parser.parse_args()

    if args.clear_cache:
        cache.clear()
        sys.exit(0)

    if not args.port or not args.origin:
        parser.error("Both --port and --origin are required unless --clear-cache is specified.")

    proxy_server.config.ORIGIN_URL = args.origin

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