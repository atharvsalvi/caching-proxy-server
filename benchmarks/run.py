"""Run from the repository root with python -m benchmarks.run."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import platform
import socket
import statistics
import threading
import time
from uuid import uuid4

import redis
from proxy_server import cache as cache_module, config, proxy


BODY = b"x" * 1024


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        with self.server.counter_lock:
            self.server.calls += 1
        time.sleep(self.server.delay)
        if self.path.startswith("/expired/"):
            if self.headers.get("If-None-Match") == '"v1"':
                self.send_response(304)
                self.send_header("Cache-Control", "max-age=3600")
                self.send_header("ETag", '"v1"')
                self.end_headers()
                return
            rules = "max-age=0"
            if self.path.startswith("/expired/background/"):
                rules += ", stale-while-revalidate=10"
        else:
            rules = "max-age=3600"
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.send_header("Cache-Control", rules)
        if self.path.startswith("/expired/"):
            self.send_header("ETag", '"v1"')
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


@contextmanager
def serve(handler):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


@contextmanager
def serve_proxy(backend, origin_url):
    if backend == "threaded":
        def refreshing():
            with proxy._registry_lock:
                return bool(proxy._refreshing)

        config.ORIGIN_URL = origin_url
        with serve(proxy.ProxyRequestHandler) as server:
            yield server.server_port, refreshing
        return

    import uvicorn
    from proxy_server.asgi import create_app

    app = create_app(origin_url)
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error", ws="none",
                                          server_header=False, date_header=False))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("FastAPI server failed to start")
                time.sleep(0.01)
            yield port, lambda: bool(app.state.refreshes)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("FastAPI server failed to stop")


def request(port, path, barrier=None):
    if barrier is not None:
        barrier.wait(timeout=15)
    start = time.perf_counter()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        elapsed_ms = (time.perf_counter() - start) * 1000
        if response.status != 200 or body != BODY:
            raise RuntimeError(f"Unexpected response for {path}: HTTP {response.status}, {len(body)} bytes")
        return {"ms": elapsed_ms, "cache": response.getheader("X-Cache")}
    finally:
        connection.close()


def summarize(samples, elapsed, origin_calls):
    times = sorted(row["ms"] for row in samples)
    counts = Counter(row["cache"] for row in samples)
    count = len(times)
    return {
        "requests": count,
        "mean_ms": statistics.mean(times),
        **{f"p{p}_ms": times[math.ceil(p * count / 100) - 1] for p in (50, 95, 99)},
        "elapsed_s": elapsed,
        "requests_per_second": count / elapsed,
        "hits": counts["HIT"],
        "misses": counts["MISS"],
        "hit_ratio": None if None in counts else counts["HIT"] / count,
        "origin_requests": origin_calls,
    }


def measure(name, origin, proxy_port, count, workers):
    if name == "warm_hit":
        if request(proxy_port, "/warm")["cache"] != "MISS":
            raise RuntimeError("Expected a cold warm-up request")
    before = origin.calls
    samples = []
    start = time.perf_counter()
    if name == "concurrent":
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for wave in range(count // workers):
                barrier = threading.Barrier(workers)
                futures = [pool.submit(request, proxy_port, f"/wave/{wave}", barrier)
                           for _ in range(workers)]
                samples.extend(future.result() for future in futures)
    else:
        port = origin.server_port if name == "direct" else proxy_port
        for i in range(count):
            path = "/warm" if name == "warm_hit" else f"/{name}/{i}"
            samples.append(request(port, path))
    summary = summarize(samples, time.perf_counter() - start, origin.calls - before)
    # Refuse to label a cache outage, expiry, or unexpectedly warm run as a valid comparison.
    expected = {
        "direct": (0, 0, count), "cold_miss": (0, count, count),
        "warm_hit": (count, 0, 0),
        "concurrent": (count - count // workers, count // workers, count // workers),
    }
    observed = (summary["hits"], summary["misses"], summary["origin_requests"])
    if observed != expected[name]:
        raise RuntimeError(f"{name}: expected (hits, misses, origin calls) {expected[name]}, got {observed}")
    return {"summary": summary, "samples": samples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=200, help="Requests per scenario")
    parser.add_argument("--workers", type=int, default=4, help="Clients per concurrent burst")
    parser.add_argument("--delay-ms", type=int, default=20, help="Origin delay per request")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--backend", choices=["fastapi", "threaded"], default="fastapi")
    parser.add_argument("--refresh-comparison", action="store_true",
                        help="Also compare bursts against immediately expired entries, with and without grace")
    parser.add_argument("--output", type=Path, help="Save settings, summaries, and raw samples as JSON")
    args = parser.parse_args()
    if args.workers < 1 or args.requests < 1 or args.requests % args.workers:
        parser.error("--requests must be positive and divisible by a positive --workers")
    if not 0 <= args.delay_ms < config.ORIGIN_TIMEOUT * 1000:
        parser.error("--delay-ms must be nonnegative and below the origin timeout")

    # This runner owns its process. Only its new, random namespace is cleared.
    cache_module.CACHE_PREFIX = f"benchmark:{uuid4().hex}:"
    cache_module.REDIS_PORT = args.redis_port
    store = cache_module.CacheStore()
    proxy.cache = store
    try:
        version = store._redis.info("server")["redis_version"]
    except redis.RedisError as error:
        store._redis.close()
        parser.exit(1, f"Redis unavailable at 127.0.0.1:{args.redis_port}: {error}\n")
    report = {
        "started_utc": datetime.now(timezone.utc).isoformat(), "os": platform.platform(),
        "backend": args.backend,
        "python": platform.python_version(), "redis": version, "redis_py": redis.__version__,
        "body_bytes": len(BODY), "requests_per_scenario": args.requests,
        "workers": args.workers, "delay_ms": args.delay_ms, "scenarios": {},
    }
    try:
        with serve(Origin) as origin, serve_proxy(
            args.backend, f"http://127.0.0.1:{origin.server_port}"
        ) as (port, refreshing):
            origin.calls = 0
            origin.counter_lock = threading.Lock()
            origin.delay = args.delay_ms / 1000
            for name in ("direct", "cold_miss", "warm_hit", "concurrent"):
                result = measure(name, origin, port, args.requests, args.workers)
                report["scenarios"][name] = result
                print(name, json.dumps(result["summary"]), flush=True)
            if args.refresh_comparison:
                for mode in ("blocking", "background"):
                    result = measure_refresh(mode, origin, port, args.requests, args.workers, refreshing)
                    report["scenarios"][mode] = result
                    print(mode, json.dumps(result["summary"]), flush=True)
    finally:
        try:
            store.clear()
        finally:
            store._redis.close()
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def measure_refresh(mode, origin, port, count, workers, refreshing):
    samples = []
    elapsed = 0
    calls = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for wave in range(count // workers):
            path = f"/expired/{mode}/{wave}"
            if request(port, path)["cache"] != "MISS":
                raise RuntimeError("Expected a new, immediately expired entry")
            before = origin.calls
            barrier = threading.Barrier(workers)
            start = time.perf_counter()
            futures = [pool.submit(request, port, path, barrier) for _ in range(workers)]
            burst = [future.result() for future in futures]
            elapsed += time.perf_counter() - start
            samples.extend(burst)
            counts = Counter(row["cache"] for row in burst)
            if mode == "blocking":
                if counts["REVALIDATED"] != 1 or counts["HIT"] != workers - 1:
                    raise RuntimeError(f"Unexpected blocking results: {counts}")
            elif not counts["STALE"] or counts["STALE"] + counts["HIT"] != workers:
                raise RuntimeError(f"Unexpected background results: {counts}")
            # Exclude background completion and verification from client latency,
            # but account for every resulting origin request before the next burst.
            deadline = time.monotonic() + 15
            while refreshing():
                if time.monotonic() >= deadline:
                    raise RuntimeError("Background refresh did not finish")
                time.sleep(0.002)
            if request(port, path)["cache"] != "HIT" or origin.calls - before != 1:
                raise RuntimeError("Expected a fresh entry and exactly one origin validation")
            calls += origin.calls - before
    summary = summarize(samples, elapsed, calls)
    summary["outcomes"] = dict(Counter(row["cache"] for row in samples))
    return {"summary": summary, "samples": samples}


if __name__ == "__main__":
    main()
