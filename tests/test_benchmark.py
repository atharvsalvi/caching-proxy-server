import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from benchmarks.run import summarize


def test_latency_and_throughput_use_different_denominators():
    samples = [{"ms": n, "cache": "MISS" if n == 1 else "HIT"} for n in range(1, 101)]
    result = summarize(samples, elapsed=2, origin_calls=1)
    assert [result[name] for name in ("mean_ms", "p50_ms", "p95_ms", "p99_ms")] == [50.5, 50, 95, 99]
    assert result["requests_per_second"] == 50
    assert result["hit_ratio"] == 0.99


@pytest.mark.redis
def test_benchmark_entry_point_runs_all_four_scenarios(live_store, tmp_path):
    output = tmp_path / "run.json"
    subprocess.run([
        sys.executable, "-B", "-m", "benchmarks.run", "--requests", "4", "--workers", "2",
        "--delay-ms", "5", "--redis-port", os.environ.get("PROXY_TEST_REDIS_PORT", "6379"),
        "--output", str(output),
    ], cwd=Path(__file__).resolve().parents[1], check=True, timeout=30)
    scenarios = json.loads(output.read_text(encoding="utf-8"))["scenarios"]
    for name, calls in {"direct": 4, "cold_miss": 4, "warm_hit": 0, "concurrent": 2}.items():
        assert len(scenarios[name]["samples"]) == 4
        assert scenarios[name]["summary"]["origin_requests"] == calls
