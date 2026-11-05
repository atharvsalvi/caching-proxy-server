# Phase 3 — Concurrent cache misses

Four clients can request the same URL in the time it takes the origin to
answer the first request. Each client has its own handler thread. Before this
phase, all four could see a MISS and fetch the page. This burst of duplicate
work is a cache stampede. An expired popular entry has the same problem as a
brand-new one.

## Follow a request

1. The handler checks eligibility as before. For an eligible GET, it reads
   Redis. A HIT can go straight back to the client.
2. On a miss, the thread joins the lock for the full cache key. This includes
   the origin, target URL, method, and supported representation preferences.
3. After acquiring the lock, the thread checks Redis again. If a previous
   holder stored a response, the thread can release the lock and send a HIT.
4. If the entry is still missing, this thread fetches from the origin. The
   usual Phase 2 checks decide whether to store the result. The fetching
   client receives a MISS. The lock is released when forwarding finishes.
5. A short registry lock protects the dictionary of active keys. Each entry
   counts threads holding or waiting for its lock. The last thread removes
   the entry in a finally block. Different keys can do network I/O concurrently.

All requests for one process share this registry. Starting a second proxy
process creates a second registry, even when both connect to the same Redis.
Two processes can therefore make two origin requests for the same cold entry.

## Failures and trade-offs

| Situation | Behavior |
| --- | --- |
| Redis GET fails, including the second lookup | Forward this request directly, skip its cache write, and return the origin response on success with X-Cache MISS. |
| Redis SET fails | Deliver the complete origin response already in memory. The next request checks Redis again. |
| Origin fetch fails | Preserve the existing gateway error behavior and release the lock. The next waiter can retry. |
| Response is private, streamed, or otherwise ineligible | Each client needs its own fetch. For initially eligible GETs these fetches can queue behind one another. |
| Cache clear fails | The CLI exits with status 1 and reports the Redis error. A partial clear can be retried. |

Redis has a one-second socket connect timeout, a one-second socket operation
timeout, and zero automatic retries. Each later request can try the cache
again. During an outage, initial lookup failures bypass coalescing and origin
traffic can rise. An ordinary cold fetch pays for a second Redis lookup in
exchange for closing the race. The per-key lock covers response delivery too,
so slow clients can delay existing waiters. New arrivals that already see a HIT
take the fast path.

## Reproduce the result

From the project root, run the Phase 3 tests in PowerShell.

```powershell
.\.venv\Scripts\python -B -m unittest discover -s tests -p test_concurrency.py -v
```

The four-client test holds each initial cache lookup at a barrier until all
four clients have observed the cold entry. It checks for exactly one origin
request, one MISS, three HITs, and four identical response bodies. It also
checks that the registry is empty afterward. The parallel-variant test makes
the English and French fetches meet at the origin, so it also detects accidental
serialization across different keys.

The suite uses local HTTP servers, in-memory cache storage, and injected Redis
exceptions. Other cases cover a failed first fetch, private responses, outage
recovery, and cleanup after an unexpected exception.

## Interview practice

1. **What is a thread here?** Each accepted client connection can run a handler
   independently. Those handlers share the process's lock dictionary.
2. **Where is the race condition?** Several threads can finish their first
   Redis read before any thread finishes fetching and storing the response.
3. **Why check Redis twice?** A MISS describes the instant of the first lookup.
   Another thread might store a result while this thread waits for the lock.
4. **Why lock by the full key?** Requests for different URLs or language variants
   can progress independently. Requests for the same representation coordinate.
5. **Why count waiters?** Deleting a lock while a thread is waiting on it could
   let a new arrival create a second lock for the same key. Both groups could
   then fetch concurrently.
6. **What if the first fetch raises an exception?** The context manager releases
   the per-key lock and updates the registry during cleanup. A waiter can take
   over and perform its own fetch.
7. **Why bypass a failed cache?** The origin can still serve a valid response.
   This preserves availability at the cost of extra load on the origin.
8. **Why does the guarantee stop at one process?** Each process has independent
   Python memory. Sharing stored values through Redis still leaves a race between
   separate processes that observe a MISS.
