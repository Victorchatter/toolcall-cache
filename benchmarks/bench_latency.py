"""toolcall-cache latency benchmark. Run: python benchmarks/bench_latency.py

Stdlib + project deps only. Two measurements:

  (a) micro  - pure cache.get (hit) and cache.put latency (SQLite), in us.
  (b) e2e    - N repeated read_file calls through the stdio proxy (cached after
               the first) vs N now() calls (never cached, always forwarded to
               the upstream subprocess), in ms/call. A real apples-to-apples
               cached-vs-uncached comparison through the actual proxy.

Writes results.json next to this file so docs/diagrams/generate.py can render
the latency chart from measured numbers, falling back to illustrative defaults
when results.json is absent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))            # import selfcheck's fake MCP server
sys.path.insert(0, str(ROOT / "src"))    # import toolcall_cache

from selfcheck import FAKE_SERVER, _recv, _send  # noqa: E402
from toolcall_cache.cache import get, init_db, put  # noqa: E402
from toolcall_cache.key import make_args_hash, make_key  # noqa: E402

N = 50  # end-to-end calls per path
TTL = 3600
REPEATED_CALLS = 10  # the chart frames "10 repeated calls"
BENCH_DIR = Path(__file__).resolve().parent


def _read_version() -> str:
    pyproject = ROOT / "pyproject.toml"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split("=")[-1].strip().strip('"')
    return "0.0.0"


def write_results(
    hit_us: float,
    put_us: float,
    cached_call_ms: float,
    uncached_call_ms: float,
    speedup: float,
) -> Path:
    """Write standardized benchmark JSON to results.json and, when a
    BENCHMARK_TAG environment variable is set, to results/<date>-<tag>.json.
    """
    payload = {
        "tool": "toolcall-cache",
        "version": _read_version(),
        "date": date.today().isoformat(),
        "results": [
            {"name": "cache_hit_latency", "unit": "us", "value": round(hit_us, 2)},
            {"name": "cache_put_latency", "unit": "us", "value": round(put_us, 2)},
            {"name": "cached_call_latency", "unit": "ms", "value": round(cached_call_ms, 3)},
            {"name": "uncached_call_latency", "unit": "ms", "value": round(uncached_call_ms, 3)},
            {"name": "cache_speedup", "unit": "ratio", "value": round(speedup, 1)},
            {"name": "repeated_calls", "unit": "count", "value": REPEATED_CALLS},
            {"name": "ttl_seconds", "unit": "s", "value": TTL},
        ],
    }
    latest = BENCH_DIR / "results.json"
    latest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    tag = os.environ.get("BENCHMARK_TAG")
    if tag:
        results_dir = BENCH_DIR / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        tagged = results_dir / f"{payload['date']}-{tag}.json"
        tagged.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return tagged
    return latest


def _cleanup_db(db_path: str) -> None:
    for ext in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(db_path + ext)
        except OSError:
            pass


def micro_bench() -> tuple[float, float]:
    """Pure SQLite cache.get (hit) and cache.put latency, in microseconds."""
    db = tempfile.mktemp(suffix=".db")
    conn = init_db(db)
    key = make_key("s", "read_file", {"path": "/x"})
    ah = make_args_hash({"path": "/x"})
    result = {"content": [{"type": "text", "text": "content of /x"}]}
    put(conn, key, "s", "read_file", ah, result, TTL)

    for _ in range(50):  # warm
        get(conn, key)
    t0 = time.perf_counter()
    for _ in range(2000):
        get(conn, key)
    hit_us = (time.perf_counter() - t0) * 1e6 / 2000

    t0 = time.perf_counter()
    for i in range(2000):
        put(conn, f"k{i}", "s", "read_file", f"a{i}", result, TTL)
    put_us = (time.perf_counter() - t0) * 1e6 / 2000

    conn.close()
    _cleanup_db(db)
    return hit_us, put_us


def e2e_bench() -> tuple[float, float]:
    """Cached vs uncached per-call latency through the real stdio proxy, in ms."""
    fake_path = tempfile.mktemp(suffix="_fake_mcp.py")
    db_path = tempfile.mktemp(suffix=".db")
    with open(fake_path, "w", encoding="utf-8") as f:
        f.write(FAKE_SERVER)
    _cleanup_db(db_path)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    proxy = subprocess.Popen(
        [
            sys.executable, "-m", "toolcall_cache", "start",
            "--transport", "stdio",
            "--upstream", f"{sys.executable} {fake_path}",
            "--db", db_path,
            "--allowlist", "read_file",
            "--ttl", str(TTL),
        ],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    try:
        _send(proxy, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        _recv(proxy)
        _send(proxy, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        # Warm both paths (handshake, first upstream + cache fill, subprocess spin-up).
        for i in range(3):
            _send(proxy, {"jsonrpc": "2.0", "id": 100 + i, "method": "tools/call",
                          "params": {"name": "read_file", "arguments": {"path": "/w.txt"}}})
            _recv(proxy)
        for i in range(3):
            _send(proxy, {"jsonrpc": "2.0", "id": 200 + i, "method": "tools/call",
                          "params": {"name": "now"}})
            _recv(proxy)

        # Cached path: N read_file with identical args -> 1 upstream + (N-1) cache hits.
        t0 = time.perf_counter()
        for i in range(N):
            _send(proxy, {"jsonrpc": "2.0", "id": 300 + i, "method": "tools/call",
                          "params": {"name": "read_file", "arguments": {"path": "/tmp/foo.txt"}}})
            _recv(proxy)
        cached_ms = (time.perf_counter() - t0) * 1000 / N

        # Uncached path: N now() -> every call forwards to the upstream subprocess.
        t0 = time.perf_counter()
        for i in range(N):
            _send(proxy, {"jsonrpc": "2.0", "id": 400 + i, "method": "tools/call",
                          "params": {"name": "now"}})
            _recv(proxy)
        uncached_ms = (time.perf_counter() - t0) * 1000 / N
    finally:
        try:
            proxy.stdin.close()
        except Exception:
            pass
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait(timeout=2)
        try:
            os.remove(fake_path)
        except OSError:
            pass
        _cleanup_db(db_path)

    return cached_ms, uncached_ms


def main() -> None:
    hit_us, put_us = micro_bench()
    cached_ms, uncached_ms = e2e_bench()
    speedup = uncached_ms / cached_ms if cached_ms > 0 else 0.0

    results = {
        "n": N,
        "ttl_s": TTL,
        "repeated_calls": REPEATED_CALLS,
        "cache_hit_us": round(hit_us, 2),
        "cache_put_us": round(put_us, 2),
        "cached_call_ms": round(cached_ms, 3),
        "uncached_call_ms": round(uncached_ms, 3),
        "speedup": round(speedup, 1),
    }
    # Keep the old simple results.json for backward compatibility while also
    # writing the standardized LocalLab benchmark format.
    out = Path(__file__).resolve().parent / "results.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    result_path = write_results(hit_us, put_us, cached_ms, uncached_ms, speedup)

    print("toolcall-cache latency benchmark")
    print("=" * 58)
    print(f"cache.get (hit) : {hit_us:8.2f} us")
    print(f"cache.put       : {put_us:8.2f} us")
    print(f"cached call   (proxy + SQLite hit) : {cached_ms:8.3f} ms")
    print(f"uncached call (proxy + upstream)   : {uncached_ms:8.3f} ms")
    print(f"speedup (uncached / cached)        : {speedup:8.1f}x")
    print("=" * 58)
    print(f"wrote {out}")
    print(f"wrote standardized results to {result_path}")
    print(f"wrote standardized results to {result_path}")


if __name__ == "__main__":
    main()