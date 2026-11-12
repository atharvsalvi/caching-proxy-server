# Phase 5 · Run and understand the test suite

The suite exercises the proxy from an HTTP client's point of view. Earlier
tests used an in-memory cache to control timing and failures. Phase 5 adds a
real Redis boundary so the suite can also verify expiry, binary response
storage, and successful clearing.

## Run from the project root in PowerShell

Install the test extra into your project environment once.

```powershell
.\.venv\Scripts\python -m pip install -e ".[test]"
.\.venv\Scripts\python -m pytest
```

The local origin and proxy start on temporary loopback ports. No external
website or separately started proxy is needed. By default, the live Redis
tests use 127.0.0.1:6379. When Redis is unavailable, pytest lists those tests
as skipped and prints the reason. To require the full suite, start Redis
and run the following command.

```powershell
.\.venv\Scripts\python -m pytest --require-redis
```

For Redis on a different local port, set `$env:PROXY_TEST_REDIS_PORT = "6380"`
in the same terminal before running pytest. To exercise only the live tests,
run `python -m pytest -m redis --require-redis` using the project environment.
To run the offline suite explicitly, use `python -m pytest -m "not redis"`.

## Coverage and purpose

| Tests | What a failure would mean |
| --- | --- |
| MISS then HIT, origin call count | Repeated GETs may fetch twice or return the wrong body. |
| Different origins, paths, queries and base paths | Two distinct resources may accidentally reuse an entry. |
| Supported Vary fields, unsupported fields, missing and repeated headers | One client's representation may be returned to another. |
| POST, PUT, DELETE, Cookie, Authorization, request cache controls | A request that needs origin handling may incorrectly read or populate the shared cache. |
| Response cache controls, Expires, Age, stale validators | A response may be reused too long or revalidated with the wrong body or metadata. |
| Origin HTTP errors, timeouts, framing, redirects and streaming | A client may get an incorrect status, corrupted body, or an incomplete cached entry. |
| Concurrent identical requests, different variants and failing leaders | A cold burst may cause redundant origin fetches, block unrelated requests, or strand waiters. |
| Redis read/write failure and recovery | A cache outage may prevent a valid origin response or leave the key locked. |

The tests are grouped by behavior in `test_cache_keys.py`,
`test_http_handling.py`, `test_concurrency.py`, and `test_freshness.py`.
The Phase 2 guide explains each HTTP test and the Phase 3 and 4 guides walk
through the concurrency and revalidation cases. Existing unittest cases are
collected by pytest alongside the new fixture-based tests.

## Four new tests in test_redis_integration.py

1. **Successful CLI clear.** Warm two URLs, verify a binary body survives a
   cache HIT, then run the actual CLI handler with `--clear-cache`. Check its
   exit status, the removal of both entries, and the survival of a key outside
   their namespace. The next GET must MISS and increment the origin count.
2. **Physical Redis expiry.** Cache an origin's two-second response without a
   validator. Inspect its real TTL, poll for disappearance within a deadline,
   and verify that the next GET goes back to the origin.
3. **Retained stale validator.** Make a response immediately stale with
   `max-age=0` and store an ETag. Verify the Redis key still exists, the
   origin sees `If-None-Match`, and a 304 supplies the original body with
   `X-Cache: REVALIDATED`.
4. **Unreachable Redis.** Point the actual Redis client at a reserved but
   non-listening local port. Two requests must each reach the origin and
   return its binary body with status 200. This exercises failure handling
   without disrupting an existing Redis instance.

Each live test gets a unique key prefix outside the production prefix and
clears only that prefix during teardown. The clear test's separate sentinel
also has a short TTL and is deleted explicitly. Tests use no database-wide
flush. The live cases exercise Redis storage and the HTTP path; the clear case
invokes the CLI entry function in-process so it uses the test namespace.

At Phase 5 verification, the strict suite passed all 59 tests, including all
three tests marked `redis`, against a temporary Redis 7 container. The outage
test runs even when no Redis service is available.

## Interview practice

1. **Why verify the origin count as well as X-Cache?** A header reports what
   the proxy thinks happened. The origin count measures whether work was avoided.
2. **Why keep the in-memory cache tests?** They let us force precise conditions,
   such as four simultaneous misses or a failed write, cheaply and repeatably.
3. **Why use real Redis for expiry?** Checking the arguments to SET verifies
   intent. Observing the key disappear verifies Redis performed the operation.
4. **How can a key exist after its HTTP lifetime ends?** Retaining its ETag
   and body lets the proxy validate it. A Redis lookup alone cannot establish
   that the body is fresh enough to serve.
5. **How do these tests avoid touching normal cache entries?** Each test gets
   a random prefix used by reads, writes, and the CLI clear operation. A
   separate sentinel makes accidental over-deletion visible.
6. **How do you tell whether the integration checks actually ran?** Inspect
   pytest's pass/skip summary. Use `--require-redis` when a complete run is
   required so an unavailable service fails the run.
