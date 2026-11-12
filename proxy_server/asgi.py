"""Async HTTP and Redis I/O with the same representation and freshness rules."""

import asyncio
from contextlib import asynccontextmanager, suppress
from email.message import Message
from email.utils import formatdate
from weakref import WeakValueDictionary

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, Request
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from starlette.requests import ClientDisconnect
from starlette.responses import PlainTextResponse, Response, StreamingResponse

from proxy_server import cache as storage, config, policy
from proxy_server.proxy import (
    MAX_BACKGROUND_REFRESHES, ProxyRequestHandler as Rules, content_length, excluded_headers,
)


def as_message(raw_headers):
    message = Message()
    for name, value in raw_headers:
        message[name.decode("ascii")] = value.decode("latin-1")
    return message


def decorate(response, headers, length, label):
    excluded = excluded_headers(headers) | {"x-cache"}
    fields = [(k.lower(), v) for k, v in headers if k.lower() not in excluded]
    if not any(k == "date" for k, _ in fields):
        fields.append(("date", formatdate(usegmt=True)))
    if length is not None and response.status_code not in (204, 304):
        fields.append(("content-length", str(length)))
    fields.extend([("x-cache", label), ("connection", "close")])
    response.raw_headers = [(k.encode("ascii"), v.encode("latin-1")) for k, v in fields]
    return response


def cached(entry, label):
    headers = [(k, v) for k, v in entry["headers"] if k.lower() != "age"]
    headers.append(("Age", str(int(policy.age(entry, policy.time())))))
    return decorate(Response(entry["body"], entry["status_code"]), headers, len(entry["body"]), label)


class OriginStream(StreamingResponse):
    """Release the origin connection even when the downstream client disconnects."""
    def __init__(self, upstream):
        self.upstream = upstream
        super().__init__(upstream.aiter_raw(), status_code=upstream.status_code)

    async def __call__(self, scope, receive, send):
        async def timed_send(message):
            async with asyncio.timeout(config.CLIENT_TIMEOUT):
                await send(message)

        try:
            await super().__call__(scope, receive, timed_send)
        finally:
            await self.upstream.aclose()


def create_app(origin, *, redis_port=None, prefix=None):
    origin = origin.rstrip("/")
    prefix = storage.CACHE_PREFIX if prefix is None else prefix
    locks = WeakValueDictionary()
    refreshes = {}

    @asynccontextmanager
    async def lifespan(app):
        async with redis.Redis(host=storage.REDIS_HOST, port=redis_port or storage.REDIS_PORT,
                               socket_timeout=1, socket_connect_timeout=1,
                               retry=Retry(NoBackoff(), 0)) as db:
            async with httpx.AsyncHTTPTransport(trust_env=False) as transport:
                app.state.db, app.state.transport = db, transport
                try:
                    yield
                finally:
                    tasks = list(refreshes.values())
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.refreshes = refreshes

    def lock_for(key):
        # No await between lookup and insertion. Holders and waiters keep
        # strong references until the last coroutine exits.
        return locks.setdefault(key, asyncio.Lock())

    async def lookup(key):
        return storage.unpack_entry(await app.state.db.get(key))

    async def store(key, entry):
        timing = {k: entry[k] for k in ("lifetime", "received_at", "initial_age")}
        payload, ttl = storage.pack_entry(entry["status_code"], entry["headers"], entry["body"], timing)
        with suppress(RedisError):
            await app.state.db.set(key, payload, ex=ttl)

    async def fetch(method, url, headers, body, key=None, stale=None, background=False):
        conditions = policy.validators(stale["headers"]) if stale else {}
        # Use the pooled transport directly. Cookies belong to individual
        # requests and redirects are returned to the caller.
        started = policy.time()
        upstream = None
        try:
            fields = httpx.Headers({**headers, **conditions}, encoding="latin-1")
            outbound = httpx.Request(method, url, headers=fields, content=body,
                                     extensions={"timeout": httpx.Timeout(config.ORIGIN_TIMEOUT).as_dict()})
            upstream = await app.state.transport.handle_async_request(outbound)
            received = policy.time()
            status = upstream.status_code
            fields = as_message(upstream.headers.raw)
            pairs = list(fields.items())
            length = content_length(fields)
            encodings = fields.get_all("Transfer-Encoding", [])
            if encodings and (length is not None or ",".join(encodings).strip().lower() != "chunked"):
                raise ValueError("ambiguous or unsupported origin framing")
            if not 200 <= status < 600:
                raise ValueError("unsupported origin status")
            if status == 304 and conditions:
                updated = Rules.merge_validated(stale, pairs, started, received)
                if updated is None:
                    with suppress(RedisError):
                        await app.state.db.delete(key)
                    await upstream.aclose()
                    return await fetch(method, url, headers, body, key, background=background)
                await store(key, updated)
                return None if background else cached(updated, "REVALIDATED")
            if status in (204, 304):
                length = 0
            if key and length is not None and length <= config.MAX_CACHE_BODY and Rules.can_store_response(status, pairs):
                data = bytearray()
                async for chunk in upstream.aiter_raw():
                    if len(data) + len(chunk) > length:
                        raise ValueError("origin body exceeds its length")
                    data.extend(chunk)
                if len(data) != length:
                    raise ValueError("incomplete origin response")
                entry = dict(policy.freshness(pairs, started, received), body=bytes(data),
                             status_code=status, headers=pairs)
                await store(key, entry)
                if not background:
                    return decorate(Response(entry["body"], status), pairs, length, "MISS")
            else:
                if stale and status < 500:
                    with suppress(RedisError):
                        await app.state.db.delete(key)
                if not background:
                    if length == 0:
                        return decorate(Response(b"", status), pairs, length, "MISS")
                    stream = decorate(OriginStream(upstream), pairs, length, "MISS")
                    upstream = None  # Transfer connection ownership to the response.
                    return stream
        except httpx.TimeoutException:
            if not background:
                return PlainTextResponse("Origin timed out", 504)
        except (httpx.HTTPError, ValueError):
            if not background:
                return PlainTextResponse("Invalid or unavailable origin response", 502)
        finally:
            if upstream is not None:
                await upstream.aclose()

    def schedule_refresh(key, url, headers):
        if key in refreshes:
            return True
        if len(refreshes) >= MAX_BACKGROUND_REFRESHES:
            return False

        async def refresh():
            try:
                async with lock_for(key):
                    entry = await lookup(key)
                    if entry and not policy.fresh(entry, {}):
                        await fetch("GET", url, headers, None, key, entry, background=True)
            except RedisError:
                pass
            finally:
                refreshes.pop(key, None)

        refreshes[key] = asyncio.create_task(refresh())
        return True

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def handle(request: Request):
        incoming = as_message(request.scope["headers"])
        try:
            length = content_length(incoming)
        except ValueError:
            return PlainTextResponse("Invalid or duplicate Content-Length", 400)
        if "Transfer-Encoding" in incoming:
            return PlainTextResponse("Unsupported request encoding", 400 if length is not None else 501)
        if "Expect" in incoming:
            return PlainTextResponse("Unsupported expectation", 417)
        if (length or 0) > config.MAX_REQUEST_BODY:
            return PlainTextResponse("Request body too large", 413)
        body = bytearray()
        chunks = request.stream().__aiter__()
        try:
            while True:
                chunk = await asyncio.wait_for(anext(chunks), config.CLIENT_TIMEOUT)
                if len(body) + len(chunk) > config.MAX_REQUEST_BODY:
                    return PlainTextResponse("Request body too large", 413)
                body.extend(chunk)
        except StopAsyncIteration:
            pass
        except TimeoutError:
            return PlainTextResponse("Request body timed out", 408)
        except ClientDisconnect:
            return PlainTextResponse("Incomplete request body", 400)
        if length is not None and len(body) != length:
            return PlainTextResponse("Incomplete request body", 400)
        target = request.scope["raw_path"].lstrip(b"/")
        query = request.scope["query_string"]
        url = origin + "/" + (target + (b"?" + query if query else b"")).decode("ascii")
        headers = Rules.build_forwarded_headers(incoming)
        if (request.method != "GET" or body or "Range" in incoming
                or any(k.lower().startswith("if-") for k in incoming) or not Rules.can_use_cache(incoming)):
            return await fetch(request.method, url, headers, bytes(body) or None)
        key = prefix + Rules.build_cache_key("GET", url, headers)
        rules = policy.directives(incoming.items())
        try:
            entry = await lookup(key)
            if policy.fresh(entry, rules):
                return cached(entry, "HIT")
            if policy.can_serve_stale(entry, rules) and schedule_refresh(key, url, headers):
                return cached(entry, "STALE")
            async with lock_for(key):
                entry = await lookup(key)
                if policy.fresh(entry, rules):
                    return cached(entry, "HIT")
                return await fetch("GET", url, headers, None, key, entry)
        except RedisError:
            return await fetch("GET", url, headers, None)

    return app
