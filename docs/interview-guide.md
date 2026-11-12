# Caching proxy interview guide

## Describe the project in one minute

“I built a Python reverse proxy that forwards HTTP requests to a configured
origin and stores eligible GET responses in Redis. Each key identifies the
origin, URL and selected request headers. A fresh hit skips the origin. For
an expired entry, the proxy can ask the origin whether the saved body is still
current. I tested correctness with local HTTP servers and real Redis, then
measured client latency and counted origin calls under concurrent load.”

## Walk a request through the code

Start with two clients asking for the same English page. Both can miss Redis.
One acquires the key's lock, fetches the origin response and stores it if
eligible. The second checks Redis again after acquiring the lock and finds
the first client's result. A French request uses its own key and can proceed
independently. Later, an expired English entry can be validated with its ETag.
When the origin explicitly permits stale-while-revalidate, clients can receive
the previous body during a fixed grace period while one worker refreshes it.

## Questions to practice

| Interview question | Answer from this implementation |
| --- | --- |
| Why include the whole origin URL? | Two origins can both serve `/products` and share one Redis instance. Including the origin and original query prevents accidental reuse. |
| Why include three headers before seeing Vary? | A fixed set lets the proxy select a key with one lookup. It costs memory and can reduce hits. Unknown Vary fields trigger bypass. |
| Why buffer some responses and stream others? | Redis storage needs a complete body. Buffering bounded eligible responses allows caching; streaming larger responses gets bytes to the client sooner with bounded reads. |
| What prevents a cache stampede? | A per-key lock plus a second Redis lookup. Without the second check, waiters could each fetch in turn. Multiple processes have independent locks. |
| How does Redis failure affect clients? | An unsuccessful read causes origin bypass. A failed write still leaves the complete origin response available to its client. The origin may receive more traffic during an outage. |
| Can an expired entry remain in Redis? | Yes. Retaining a validator and body gives the origin the option to confirm it with a 304. Freshness controls reuse and Redis expiry controls retention. |
| Why is a background refresh sometimes faster? | Clients can finish before the origin replies. They accept the risk of seeing an older representation during an origin-authorized window. |
| How did you test concurrency? | A barrier made multiple requests observe a cold entry. Tests checked the response bodies, cache labels, and the number of origin calls. |
| What does the benchmark establish? | In the recorded local baseline, warm hits averaged 3.52 ms and direct requests 32.11 ms. Cold misses averaged 69.84 ms. These numbers describe that host and workload. |
| Why run tests against real Redis in CI? | A mock can assert the TTL argument. The strict suite also observes physical expiry, successful namespace clearing, and retention of a stale validator. |
| What did packaging cleanup accomplish? | The install contains the proxy package and its Redis client dependency. Generated copies and bytecode can be recreated locally. CI installs the package, checks its CLI and exercises the full test suite. |

## Follow-up material

- [Cache keys and Vary](phase-1b-vary.md)
- [HTTP framing, timeouts and streaming](phase-2-http.md)
- [Thread coordination and Redis fallback](phase-3-concurrency.md)
- [Freshness and validators](phase-4-freshness.md)
- [Tests and isolation](phase-5-testing.md)
- [Benchmark methodology and recorded results](phase-6-benchmark.md)
