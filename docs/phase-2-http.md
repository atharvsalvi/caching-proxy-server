# Phase 2: HTTP request and response handling

## Why this phase was needed

The original handler read every request and response body into memory, used no
explicit socket timeout, automatically followed redirects, and excluded only four
headers. A large download could consume large amounts of memory, a stalled origin
could occupy a thread indefinitely, and malformed body lengths could interrupt a
request handler. Forwarding connection-specific metadata could also misdescribe
the separate connection between the proxy and its client.

## Changes and request flow

The architecture remains ThreadingHTTPServer, urllib, and Redis. There are no new
dependencies. The small NoRedirect subclass is urllib's extension point for
returning redirects instead of following them.

1. `setup()` applies the client socket timeout, including reads while parsing headers.
2. Before any cache lookup, validate Content-Length and reject duplicate, negative,
   nondecimal, or comma-separated lengths with 400. This deliberately rejects even
   equal duplicates rather than attempting to resolve them.
3. Reject request Transfer-Encoding with 501, or 400 if Content-Length is also
   present. Reject Expect with 417. Chunked uploads and the 100-continue handshake
   are not implemented. Close the connection after these responses.
4. Reject bodies above the request limit with 413 before allocating them. Buffer
   accepted bodies up to the limit; a short body gets 400 and a stalled body gets
   408. Missing Content-Length means no request body.
5. Prepare outgoing headers and the cache key. Remove the fixed hop-by-hop header
   set plus every header named by Connection. urllib regenerates Host and request
   Content-Length for the outgoing connection. Supported Vary fields are combined
   only if they were not removed. Accept-Encoding still defaults to identity.
6. GET requests with a body, Range, or an If-* field bypass caching. Partial 206 and
   conditional 304 responses are not stored. This avoids treating them as ordinary
   full representations before conditional caching is implemented in Phase 4.
7. Eligible hits return the cached body with a reconstructed length. Keys now use
   v4, avoiding old entries created under automatic redirect following and earlier
   forwarding rules. The Redis interface and TTL remain unchanged.
8. On misses, use a direct urllib opener with an origin socket timeout. Environment
   HTTP proxy settings are deliberately ignored. A redirect or origin HTTP error
   is treated as a response to forward, rather than as a proxy failure.
9. Validate origin framing: reject invalid/duplicate lengths, unsupported transfer
   encodings, and Transfer-Encoding plus Content-Length. Support ordinary chunked
   origin responses using Python's decoder. Trailers are discarded, not forwarded.
10. Buffer a response only when it is eligible for storage and has a known length
    at or below the cache-body limit. Verify that the expected bytes arrived before
    storing it. Buffering lets an incomplete small response become a clean 502.
11. Stream other responses with `read1()` in chunks of at most 64 KiB. This permits
    forwarding available bytes without waiting to fill a whole chunk. Write each
    chunk before reading the next. Large, unknown-length, and uncacheable responses
    are not stored. Content-Encoding and compressed body bytes remain unchanged.
12. Remove response hop-by-hop fields and any origin X-Cache value. Emit one local
    X-Cache header and Connection: close. Known lengths are forwarded/reconstructed;
    unknown-length streams end when the connection closes. 204 and 304 have no body
    and no generated Content-Length.

Origin timeout before response headers are sent produces 504. Other supported
origin network/protocol failures produce 502. If streaming has already begun, the
proxy closes the connection instead: it cannot replace the status already sent.
Client disconnects during output close the origin response through its context
manager. Redis-failure fallback is still Phase 3 work.

## Buffering versus streaming

Buffering waits for the complete body and retains it before returning it. It is
convenient for the existing Redis serialization and permits validating a small
body before sending success. Streaming forwards available chunks progressively,
which reduces body memory usage and lets the client start receiving data earlier.

Memory is bounded per response read, not for the whole process: concurrent threads,
socket buffers, cached hits, and Redis serialization still consume memory. Cached
bodies are hex-encoded by the existing CacheStore, which increases storage size.

## Configuration and usage

| CLI flag | Default | Meaning |
| --- | --- | --- |
| --origin-timeout | 10 seconds | Origin connection and blocking socket-operation timeout |
| --client-timeout | 10 seconds | Client blocking socket-operation timeout |
| --max-request-body | 1048576 bytes | Maximum buffered upload size |
| --max-cache-body | 1048576 bytes | Maximum buffered/cacheable origin response size |

Timeouts must be finite and positive. Sizes must be positive integers.
Defaults also live in config.py for programmatic server use. The streaming chunk
size is 64 KiB in config.py; it is not a CLI flag.

Restart the proxy to load these changes. For example:

```powershell
.\.venv\Scripts\python -m proxy_server.cli --port 8080 --origin https://httpbin.org --origin-timeout 5 --client-timeout 10 --max-request-body 1048576 --max-cache-body 1048576
```

An absolute Location is returned unchanged, so following it can take a client
directly to the origin. A relative Location is also unchanged. Redirect rewriting
and cookie/domain rewriting are not implemented.

## Automated verification

```powershell
.\.venv\Scripts\python -B -m unittest discover -s tests -v
```

33 tests passed at completion of Phase 2: 16 existing tests plus 17 HTTP tests.
All network tests use local servers and in-memory cache storage, not external
websites or a live Redis instance.

| Test | Purpose |
| --- | --- |
| Request framing errors | Invalid lengths, TE, oversized bodies, and Expect never reach origin |
| Short/stalled uploads | Distinguish incomplete uploads from client timeouts |
| POST forwarding | Preserve bytes and reconstruct outgoing length |
| Small response MISS/HIT | Preserve caching and correct downstream length |
| Large/unknown responses | Preserve complete bodies without storing them |
| Progressive streaming | Receive first byte while origin is blocked before sending the remainder |
| Chunked origin | Decode chunks and remove transfer/trailer headers |
| Invalid origin framing | Return 502 without populating cache |
| Origin timeout/refusal | Return gateway errors instead of hanging |
| Redirect | Return Location and never request its destination internally |
| Origin 404 | Preserve status and error body |
| Hop-by-hop fields | Filter both fixed and Connection-nominated fields |
| Body/Range/conditional GET | Bypass an already populated ordinary entry |
| Bodyless statuses | Avoid body and generated Content-Length on 204/304 |
| CLI limits | Reject zero, negative, and nonfinite settings |
| Midstream timeout | Close a partial response without appending another HTTP response |
| Compressed representation | Preserve gzip bytes and metadata on MISS and HIT |

## Limitations and trade-offs

- Socket timeouts are not total request deadlines. Slow trickles may keep a
  request active, and DNS lookup time is not bounded by the socket timeout.
- Uploads remain buffered, but are size-limited. Chunked uploads are rejected.
- Small unknown-length responses also bypass caching to keep the implementation
  simple. The proxy does not buffer a prefix to discover whether they are small.
- Responses use HTTP/1.0 with explicit connection closure; no downstream connection
  reuse or chunked output is implemented. Known-length truncation is detectable by
  clients; close-delimited truncation cannot always be distinguished from success.
- Forwarding a redirect preserves origin behavior but may move subsequent client
  requests outside the proxy. Use a client without automatic redirects to inspect it.
- Responses with unusual transfer encodings are rejected instead of decoded.
- Existing cache semantics remain limited; Phase 4 covers freshness/revalidation.
- Redis failure handling, stampede control, and performance benchmarks remain in
  later phases. No live Redis or external HTTP tests were claimed here.

## Interview questions and answers

1. **Why use both body limits and timeouts?** A body limit bounds buffered data;
   a timeout limits waiting on a stalled socket operation. Neither replaces the other.
2. **Does streaming mean zero memory use?** No. It bounds the body data retained
   at a time, while sockets, threads, and other operations still need memory.
3. **Why not cache streamed responses?** Our Redis API requires a complete body.
   Retaining every streamed chunk would defeat the intended body-memory bound.
4. **Why reject conflicting body framing?** Different recipients could interpret
   message boundaries differently. Rejecting ambiguity avoids forwarding a request
   with inconsistent length information.
5. **What is a hop-by-hop header?** Metadata for one connection rather than the
   end-to-end response. The proxy manages two separate connections and must remove
   such fields, including extra names listed by Connection.
6. **How are transfer encoding and content encoding different?** Chunked transfer
   encoding describes message framing and is decoded by the HTTP client. Gzip
   content encoding describes the representation; its bytes and header are preserved.
7. **Why return redirects instead of following them?** It preserves the origin's
   redirect response and avoids silently fetching another URL or rewriting methods.
8. **Why can't a midstream timeout return 504?** The client already received the
   original status and part of its body. A second status line would corrupt that
   response, so the proxy closes the connection.
9. **Is a 10-second socket timeout a 10-second request deadline?** No. It applies
   to blocking socket operations; a series of successful operations can take longer.
10. **Why bypass Range and conditional requests for now?** They require special
    rules for partial responses and validators. Reusing an ordinary cached response
    without those rules can produce incorrect behavior.

References:
- https://www.rfc-editor.org/rfc/rfc9112.html#section-6.3
- https://docs.python.org/3/library/urllib.request.html
