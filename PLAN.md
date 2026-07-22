# toolcall-cache — Implementation Plan

## Goal
Ship a local, content-addressed cache for agent tool results. It sits as a tiny MCP proxy between any MCP-using agent and any MCP server, returning cached results for repeated deterministic `(tool, args)` calls instead of re-running them. Result: less repeat spend and lower latency in long sessions.

## Locked design decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Cache key | `sha256(canonical_json({server_id, tool_name, args}))` | Same-named tools on different upstream servers must not collide. |
| Default policy | Allowlist-first | Only cache tools explicitly named via `--allowlist` or annotated `cacheable: true`. Safe-by-default; a mistake means a miss, not a wrong answer. |
| Denylist | Always wins | Patterns `*_write*`, `send*`, `delete*`, `random*`, `time*`, `now*`, `date*` never cache, even if allowlisted. Configurable via `--denylist`. |
| Tool annotation | `annotations.cacheable == true` | MCP tools may self-declare cacheability; proxy honors it when present. |
| Storage | One SQLite file | `~/.toolcall-cache/toolcall-cache.db` by default. No server, no telemetry. |
| TTL | Per-entry, default 1 hour | `--ttl <seconds>` on `start`. Expired entries are ignored and overwritten on next upstream hit. |
| Transports | stdio + HTTP | stdio spawns the upstream server as a subprocess. HTTP runs a tiny reverse proxy. Both stdlib-only. |
| Out of scope | Semantic caching, shared locking, web UI, auto-detect cacheability | YAGNI for v1. |

## Architecture

```
Agent / MCP client
        │
        ▼
┌─────────────────────┐
│  toolcall-cache     │  ← decides cacheability, builds key, reads/writes SQLite
│  (MCP proxy)        │
└─────────────────────┘
        │
   ┌────┴────┐
   ▼         ▼
stdio     HTTP
   │         │
   ▼         ▼
upstream  upstream
MCP srv   MCP srv
```

### Module map
- `src/toolcall_cache/cache.py` — SQLite schema, get/put/clear/invalidate/stats.
- `src/toolcall_cache/key.py` — Canonical JSON serialization + SHA256.
- `src/toolcall_cache/policy.py` — Allowlist, denylist, annotation check.
- `src/toolcall_cache/proxy.py` — JSON-RPC message classifier and cache decision.
- `src/toolcall_cache/transports/stdio.py` — Stdio proxy loop.
- `src/toolcall_cache/transports/http.py` — HTTP reverse proxy.
- `src/toolcall_cache/server.py` — Transport selection + startup wiring.
- `src/toolcall_cache/cli.py` — `toolcall-cache` CLI.
- `selfcheck.py` — End-to-end test with a fake MCP server.

## Implementation phases

1. **Bootstrap** — `pyproject.toml`, `LICENSE`, `.gitignore`, package skeleton, entry point `toolcall-cache`.
2. **Core primitives** — `key.py`, `policy.py`, `cache.py` with small focused unit tests via `selfcheck.py` helpers.
3. **Stdio transport** — Spawn fake upstream, proxy non-tool traffic, intercept `tools/call`, return cached responses directly.
4. **HTTP transport** — Lightweight reverse proxy using `http.server` + `urllib.request`; cache only explicit `tools/call` JSON-RPC POSTs.
5. **CLI** — `start`, `clear`, `invalidate`, `list`, `stats` subcommands.
6. **Selfcheck** — Fake MCP server with `read_file` (cacheable) and `now` (non-cacheable). Assert `read_file` second call hits cache (upstream counter == 1) and `now` always passes through (counter increments).
7. **Diagrams & README** — Mermaid architecture/sequence diagrams, generated SVG before/after charts, professional README with methodology.
8. **Final verification** — `python -m pip install .`, `python selfcheck.py`, README links work.

## Deliverables
- `pipx install .` works, `toolcall-cache --help` works.
- `python selfcheck.py` exits 0.
- README explains what problem this solves, how it fixes it, and exact usage.
