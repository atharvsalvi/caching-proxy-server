# Phase 6 · Measuring the caching proxy

A passing HIT/MISS test tells us that the proxy served the right body. To learn
what that costs a client, the benchmark times complete HTTP requests and counts
the calls the origin actually receives.

## Reproduce the measurement

Start Redis, then run from the repository root in PowerShell:

```powershell
.\.venv\Scripts\python -B -m benchmarks.run --output my-run.json
```

The runner starts its own loopback origin and production proxy handler on
ephemeral ports. Redis defaults to 127.0.0.1:6379. To use a dedicated Redis
container when Docker Desktop is running:

```powershell
docker run --rm -d --name caching-proxy-bench -p 127.0.0.1:6380:6379 redis:7
.\.venv\Scripts\python -B -m benchmarks.run --redis-port 6380 --output my-run.json
docker stop caching-proxy-bench
```

Each run uses a unique Redis key prefix and clears that prefix on exit.
The defaults are 200 requests per scenario, four concurrent clients per burst,
a fixed 1 KiB response and 20 ms of simulated work at the origin. Adjust
`--requests`, `--workers` and `--delay-ms` to try another workload. The request
count must be divisible by the worker count. For example, repeat with
`--delay-ms 0` to see what happens when a cache hit saves very little origin work.

## What happens in each scenario

1. **Direct.** One client requests a distinct URL each time from the origin.
2. **Cold MISS.** One client requests distinct URLs through the proxy. Each
   incurs a Redis lookup, a recheck under the key lock, an origin fetch and a
   cache write.
3. **Warm HIT.** First populate one URL. Exclude that warm-up request and its
   origin call from the measurement. Repeated GETs now read the body from Redis.
4. **Concurrent cold bursts.** Four clients synchronize at a barrier and request
   the same new URL. Wait for all four to finish, then repeat for another URL.
   With 200 requests this gives 50 cold URLs. The proxy can fill each once.
   Some clients may see an entry already filled by the time they reach the handler.

The client opens a new HTTP connection per GET and measures from connection
setup through reading the last byte with a monotonic performance clock. Successful
samples must return 200 and the expected body. Unexpected bodies, errors or
unexpected HIT/MISS/origin counts fail the run so a broken cache cannot produce a
successful benchmark report. A failed run needs investigation before comparison.

Mean latency is the arithmetic average. Percentiles use nearest rank on sorted
client times. Throughput divides completed requests by elapsed scenario wall time,
including burst scheduling. Hit ratio is HITs divided by measured proxy requests.
Direct requests have no cache ratio. The origin increments a separate counter
under a lock on each GET. The JSON report includes per-request measurements.

## Measured baseline

November 12, 2026. Windows 11, Python 3.13.4, redis-py 8.1.0, Redis 7.4.12
in Docker Desktop. The origin, client, and proxy ran on the same machine, with
the client and HTTP servers in one Python process. One run, default workload.

| Scenario | Mean ms | p50 ms | p95 ms | p99 ms | Requests/s | Hit ratio | Origin calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Direct | 32.11 | 33.25 | 45.40 | 47.58 | 31.12 | — | 200 |
| Cold MISS | 69.84 | 68.03 | 85.76 | 100.00 | 14.32 | 0% | 200 |
| Warm HIT | 3.52 | 1.59 | 17.49 | 24.42 | 283.42 | 100% | 0 |
| Concurrent cold bursts | 59.73 | 63.39 | 70.22 | 71.63 | 65.26 | 75% | 50 |

Source: [recorded samples and environment](../benchmarks/sample-run.json).

A warm hit avoided a trip to the delayed origin. Its mean fell from 32.11 ms
direct to 3.52 ms. A cold miss paid for both the origin work and the proxy
path, averaging 69.84 ms. During bursts, origin work fell to one request per
four clients. Their median latency still included time waiting for the first
fetch. The measurements give end-to-end times. Attributing an exact number of
milliseconds to the lock, Redis or Python scheduling would require internal timing.

The 20 ms delay, body size, key popularity, Windows scheduling, Docker
networking and fresh connections all influence these numbers. The burst workload
waits for each group to finish, so its throughput measures this load pattern.
Production capacity requires a representative arrival pattern, deployment and
more runs. With 200 samples per scenario the p99 is the 198th sorted request
and is sensitive to individual slow samples.

## Interview practice

### Follow-up experiment with expired entries

Run the original four scenarios and the two additional comparisons with
`python -m benchmarks.run --refresh-comparison --output my-refresh-run.json`.
Each comparison primes 50 distinct keys with an ETag and max-age=0 so the next
four-client burst immediately observes expiry. The background case adds
stale-while-revalidate=10. Each 304 grants a fresh lifetime. After each burst,
the runner waits for refresh completion and checks for a fresh HIT and exactly
one origin validation. Priming, completion polling and verification requests
are outside the timed client bursts. Reported throughput for these two cases
uses only the burst intervals.

| Expired entry, 200 clients per mode | Mean ms | p50 ms | p95 ms | p99 ms | Origin validations |
| --- | ---: | ---: | ---: | ---: | ---: |
| Blocking | 58.30 | 61.79 | 69.56 | 71.01 | 50 |
| Background refresh | 12.18 | 4.30 | 28.84 | 30.53 | 50 |

The blocking case returned 50 REVALIDATED and 150 HIT responses. All 200
background responses were STALE. Faster responses in this run came from
letting clients finish while validation was outstanding. Every stale response
includes its current Age. Its old body can differ from the current origin body.
Source: [full rerun with individual timings](../benchmarks/stale-refresh-run.json).

The proxy permits four concurrent background refreshes per process and one per
key. When the pool of slots is full, another key validates synchronously.
Requests with stricter freshness requirements and responses with no-cache,
must-revalidate, proxy-revalidate or s-maxage take the conservative validation
path. Expiry of the grace window forces synchronous validation regardless of
traffic or prior refresh failures. Redis retains entries through the grace
window even when a full fetch is needed because the origin supplied no validator.

### Questions

1. **Why compare three sequential paths?** Direct measures the origin baseline,
   a miss measures the cost of going through the proxy to the origin, and a
   warm hit measures reuse.
2. **Why count calls at the origin?** The count independently verifies that a
   HIT actually saved work and shows how much traffic the backend handled.
3. **How is p95 different from the mean?** The mean averages all requests.
   The p95 gives a threshold reached within the measured time by at least
   95% of the sample, revealing delays that an average can hide.
4. **Why can a concurrent HIT take a long time?** A client can wait for a
   different thread to populate Redis before it reads the entry as a HIT.
5. **Why calculate throughput from wall time?** Concurrent requests overlap.
   Adding their durations would count the same period multiple times.
6. **Why exclude warm-up?** Priming a key contacts the origin. Counting it
   as a warm hit would mix two different paths in the same sample.
7. **How could you evaluate a future Kafka publisher?** Repeat this workload
   with the publisher enabled using the same settings and compare several
   runs, including tail latency and any dropped events.
