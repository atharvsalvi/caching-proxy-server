# Phase 1B: Basic Vary-aware caching

## Problem and approach

Phase 1 avoided incorrect representation reuse by refusing to store responses
with nonempty Vary. That was safe but meant even repeated English requests always
contacted the origin.

The proxy now supports Vary on Accept, Accept-Language, and Accept-Encoding.
It includes all three outgoing request-header values in every cache key. This
lets each combination have its own Redis entry without a separate variant index.
The existing CacheStore interface, response format, and 60-second TTL are unchanged.
No dependency was added.

RFC 9111 section 4.1 requires matching the request fields named by Vary before
reusing a representation. Vary: * never matches. This implementation supports a
limited set of fields; it does not implement the entire HTTP caching standard.
Reference: https://www.rfc-editor.org/rfc/rfc9111.html#section-4.1

## Request flow and important changes

1. Build the target URL as before, preserving the path and query string.
2. Build the outgoing headers with lowercase names. Combine repeated supported
   fields in their original order using a comma and a space. These fields all
   support list values. Do not lowercase their values or sort preferences.
3. If Accept-Encoding is missing, explicitly send identity, matching the existing
   urllib/http.client default. Key the same value that the origin receives.
4. Build a v3 key from the method, complete target URL, and the three supported
   field names and values. Missing Accept or Accept-Language is JSON null; an
   explicitly empty value is an empty string, so those requests remain distinct.
5. Apply the existing request eligibility checks. An eligible cache hit returns
   the stored response without contacting the origin.
6. On a miss, forward the prepared headers and receive the origin response.
7. Apply the existing response eligibility checks and examine every Vary line.
   Store only if all nonempty members name supported fields. Unknown fields and
   the wildcard prevent storage. Empty Vary values do not prevent storage.
8. Return the response. A stored variant can satisfy later matching requests.

The v3 prefix prevents reading v1/path-only or v2 entries under the new rules.
Old entries expire normally, and the existing clear-cache command covers all versions.

## Verification

Run from the project root:

```powershell
.\.venv\Scripts\python -B -m unittest discover -s tests -v
```

The suite has 16 tests, using local HTTP servers and an isolated in-memory cache.
It does not require an external website or Redis server. It verifies:

- English and French responses remain separate and each is reused on a repeat.
- All three supported preferences participate in matching, including combinations.
- Repeated request fields are forwarded and keyed consistently.
- Missing, empty, and explicit language preferences remain distinct.
- Missing encoding becomes identity, while empty encoding remains separate.
- Wildcard, unsupported, and mixed supported/unsupported Vary values bypass storage.
- Unrelated request headers do not create additional entries.
- Existing origin isolation, query preservation, bypass rules, and old-key rejection.

These tests exercise HTTP requests but replace Redis storage; they do not verify
live Redis availability or TTL expiration.

## Manual check with the existing httpbin setup

Stop the proxy with Ctrl+C and restart it to load the changes. Redis must be running.

```powershell
.\.venv\Scripts\python -m proxy_server.cli --port 8080 --origin https://httpbin.org
```

In a separate terminal, clear old entries and make four requests within 60 seconds:

```powershell
.\.venv\Scripts\python -m proxy_server.cli --clear-cache
curl.exe -i -H "Accept-Language: en" "http://127.0.0.1:8080/response-headers?Vary=Accept-Language"
curl.exe -i -H "Accept-Language: fr" "http://127.0.0.1:8080/response-headers?Vary=Accept-Language"
curl.exe -i -H "Accept-Language: en" "http://127.0.0.1:8080/response-headers?Vary=Accept-Language"
curl.exe -i -H "Accept-Language: fr" "http://127.0.0.1:8080/response-headers?Vary=Accept-Language"
```

Expected X-Cache sequence: MISS, MISS, HIT, HIT, assuming otherwise cacheable
responses from the external service. This endpoint supplies Vary for demonstration;
it does not translate its body. The local automated language test checks distinct bodies.

## Trade-offs and limitations

- Keying all three fields even without Vary can produce unnecessary entries and
  lower the hit rate. It avoids variant discovery metadata and additional Redis calls.
- Only three fields are supported. Vary: User-Agent and other unsupported names
  still bypass storage.
- Values use exact matching after repeated-line combination. Equivalent preference
  strings may have separate entries; the proxy does not parse quality weights.
- Different variants use separate Redis keys and each has the existing TTL. Many
  distinct preferences can increase memory use; there is no variant-count limit.
- The origin must correctly describe its representation selection. Changes in
  origin policy cannot be discovered until a request reaches it again.
- Existing redirect handling, freshness limitations, and cache stampedes remain
  for later phases. This phase makes no claim of full RFC compliance.

## Interview questions and answers

1. **What does Vary describe?** Request-header fields that affected the origin's
   response selection, such as Accept-Language for language selection.
2. **How do English and French responses avoid overwriting each other?** Their
   outgoing Accept-Language values differ, so their Redis keys differ.
3. **Why key supported headers before seeing Vary?** A hit must be identifiable
   before fetching from the origin. A fixed supported set avoids a separate
   lookup for the origin's variant policy, at the cost of extra entries.
4. **Why bypass unsupported Vary fields?** Their values are absent from the key,
   so storing the response could allow incorrect reuse.
5. **Why distinguish an absent header from an empty header?** The origin can
   interpret them differently. Both the key and forwarding must preserve this
   distinction, except when the outgoing client deliberately supplies a default.
6. **Why make Accept-Encoding: identity explicit?** The origin sees this default
   even when the incoming request omits the header. Cache matching must reflect
   the actual outgoing request.
7. **Why does Vary: * still bypass storage?** A normal request-header comparison
   cannot establish a match for this wildcard.
8. **Does variant caching solve cache stampedes?** No. Several threads can still
   miss the same variant simultaneously. Request coalescing belongs to Phase 3.
