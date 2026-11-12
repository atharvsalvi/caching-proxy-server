# Phase 4 · Freshness and conditional requests

Imagine caching a product price. The origin returns a price of 100 with
`Cache-Control: max-age=5` and `ETag: "price-1"`. The previous proxy could
reuse that price for 60 seconds. This version can serve it immediately during
the first five seconds of its age. At the five-second boundary, it must ask
the origin again.

| Origin's answer to the next request | What the client receives |
| --- | --- |
| 304, confirming the same representation | The original successful status and saved price, with refreshed headers and `X-Cache: REVALIDATED` |
| 200 with a new price | The replacement price with `X-Cache: MISS`; it is stored if eligible |
| Error | The origin or gateway error. An expired price is never used as a fallback |

## How a request works

1. Redis returns the entry together with its receipt time, initial age and
   freshness lifetime. New entries use a v5 key so old entries with missing
   timing data expire independently.
2. The handler tests the entry's current age twice when necessary, first on
   lookup and then under the Phase 3 lock. This lets one successful validation
   make the entry fresh for waiting clients.
3. A stale entry with a valid ETag supplies `If-None-Match`. A usable
   Last-Modified supplies `If-Modified-Since`. The proxy sends both when
   available. Without a validator, it fetches the complete representation.
4. A matching 304 supplies new metadata. Fields provided by the origin replace
   their stored counterparts; missing metadata is retained, except that age
   and date are established for the new validation. The saved body and its
   original status go to the client. A conflicting validator, incompatible
   body metadata or storage prohibition triggers an unconditional fetch.
5. A complete response goes through the existing size, framing and variant
   checks. If the replacement is private, prohibited, or too large to store,
   the superseded entry is discarded. Redis failures retain the Phase 3
   best-effort behavior.

## Rules implemented

| Input | Policy |
| --- | --- |
| Response `s-maxage`, `max-age`, `Expires` | Prefer the shared-cache lifetime, then general lifetime, then the expiry date relative to Date. With none present, use the existing 60-second heuristic for eligible statuses. |
| Response `Age`, `Date` | Initial age accounts for apparent age and upstream Age plus request delay. Resident time increases age after receipt. Cached responses supply an updated Age. |
| Response `no-store` or `private` | Do not store. Treat field-qualified private conservatively as applying to the whole response. |
| Response `no-cache` | Always validate before reuse. Keep a copy only if there is a usable validator. Treat a qualified no-cache conservatively as applying to the whole response. |
| Response `stale-while-revalidate=N` | For up to N seconds beyond freshness, an eligible request may receive the saved body with `X-Cache: STALE` while one background thread refreshes its key. The deadline is measured from expiry. |
| `must-revalidate` or `proxy-revalidate` | Fresh entries can HIT. The proxy requires origin contact for every stale entry, including during an origin outage. |
| Request `max-age`, `min-fresh` | May force an earlier validation or full fetch. Zero max-age forces origin contact. |
| Request `no-cache`, `no-store`, Authorization or Cookie | Preserve the existing conservative bypass: go to the origin without reading or writing shared cache data for this request. |
| Missing or invalid freshness values | Invalid or repeated numeric lifetime directives, and invalid Expires, give zero freshness. |

## Storage decisions and scope

- **Freshness** determines when the proxy may serve an entry without origin
  contact. **Retention** determines when Redis may discard it. Redis keeps a
  validator-bearing entry for its remaining freshness plus five additional
  minutes. An entry that is already stale when stored gets five minutes of
  retention. Redis eviction or expiration always falls back to a full fetch.
- This extra storage can save bandwidth on a 304. Validation still incurs an
  origin round trip. An origin that always sends no-cache can therefore receive
  a request on every reuse.
- The heuristic applies to eligible 200, 203, 204, 300, 301 and 308 responses.
  Other eligible statuses need an explicit lifetime. Phase 2 limits still
  apply to large, unknown-length and partial responses.
- This phase implements shared-cache freshness for eligible GET requests.
  Client-supplied preconditions and range requests pass through. Unsafe-method
  invalidation across URL variants, offline serving beyond explicit grace,
  stale-if-error, only-if-cached, and general RFC compliance are outside this
  implementation. Changed content can remain visible until the end of a valid
  freshness window. The calculations assume a reasonably synchronized clock.

The reference is [RFC 9111](https://www.rfc-editor.org/rfc/rfc9111.html),
especially sections 4.2 (freshness and age), 4.3 (validation), and 5.2
(cache-control directives).

## Run the experiment

```powershell
.\.venv\Scripts\python -B -m unittest discover -s tests -p test_freshness.py -v
```

The tests run a local HTTP origin and proxy. A controlled clock reaches the
exact expiry boundary without sleeping. They verify the actual conditional
headers at the origin, bodies received by the client, Age, changed policies,
failed validation, and coalescing across four stale lookups. A storage test
checks the Redis payload and expiry argument through a mocked Redis client.

## Interview practice

1. **What is the difference between fresh and stored?** Fresh means reuse is
   allowed immediately. Stored means the bytes still exist, possibly only to
   support validation.
2. **Who chooses between max-age and s-maxage?** This proxy is a shared cache,
   so it prefers s-maxage. A browser's private cache can use max-age instead.
3. **Why pay attention to Age?** A response may have spent time in another cache.
   Giving it its entire lifetime again could serve it past the origin's limit.
4. **How do no-cache and no-store differ?** No-cache allows storage but requires
   validation before reuse. No-store prohibits storage.
5. **What is an ETag?** An origin-defined identifier for a representation.
   Sending it back lets the origin confirm or replace the saved version.
6. **Why return a body after an origin 304?** The proxy sent the condition on
   behalf of an ordinary GET client. The 304 confirms the proxy's stored
   representation; that representation is what the client asked for.
7. **What does must-revalidate do during an outage?** After expiry, the client
   gets an error when the origin cannot validate the entry.
8. **Why validate under the per-key lock?** Several threads can observe the
   same stale entry. Rechecking after acquiring the lock lets them share a
   successful refresh when the origin grants a new freshness window.
