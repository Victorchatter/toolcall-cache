# toolcall-cache benchmarks

This directory measures the latency of cache hits, cache puts, and cached vs.
uncached tool calls through the real stdio proxy.

## Latest results

See [`results.json`](results.json) for the most recent run in the standardized
LocalLab benchmark format.

Historical results are stored in [`results/`](results/).

## Run locally

```bash
python benchmarks/bench_latency.py
```

The script reports:

- `cache.get` hit latency (microseconds, pure SQLite)
- `cache.put` latency (microseconds, pure SQLite)
- cached call latency through the proxy (ms)
- uncached call latency through the proxy (ms)
- speedup ratio (uncached / cached)

## Latest numbers

| Metric | Value |
|---|---|
| Cache hit latency | see `results.json` |
| Cache put latency | see `results.json` |
| Cached call latency | see `results.json` |
| Uncached call latency | see `results.json` |
| Speedup | see `results.json` |

Results are measured on `ubuntu-latest` in CI and committed to `results/` only
on release tags.
