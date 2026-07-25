# toolcall-cache — design spec

**Date:** 2026-07-25
**Status:** Approved (implemented — this spec is written post-hoc from the shipped v0.1.0 to close the docs/superpowers convention gap)
**One-liner:** A local, content-addressed cache for MCP tool results. Sits as a tiny stdio/HTTP MCP proxy between any MCP-using agent and any MCP server. Repeated calls to deterministic, allowlisted tools with the same arguments return the cached result instead of re-hitting the upstream server.

## Problem

Long agent sessions repeat the same tool calls constantly: re-reading the same
`README.md` on every planning loop, re-grepping the same symbol, re-fetching
the same HTTP endpoint for context. Each repeat costs time, tokens, and — for
paid or rate-limited MCP servers — money. MCP has become the standard tool
layer across Claude Code, Codex, Cursor, and other agent frameworks, but there
is no framework-agnostic cache that sits underneath all of them. Every
framework that wants this benefit has to build its own cache, and none of
them interoperate.

## Goal

Ship a single local, content-addressed cache that works as a transparent MCP
proxy for *any* MCP client and *any* MCP server — no framework-specific
integration required. The agent's MCP config points at `toolcall-cache`
instead of the real server; the cache decides, per call, whether to answer
from SQLite or forward upstream.

## Non-goals

- Semantic / embedding-based caching (fuzzy match on similar-but-not-identical
  arguments). v1 is exact content-addressed matching only.
- Multi-agent shared locking or coordination across concurrent proxy
  instances writing to the same DB beyond SQLite's own concurrency handling.
- A web UI or dashboard. CLI only.
- Automatic cacheability detection (static analysis of a tool's side effects,
  ML classification, etc.). Cacheability is either declared by the operator
  (`--allowlist`) or self-declared by the MCP server (`annotations.cacheable`).
- Caching non-`tools/call` traffic. `initialize`, `tools/list`, resources,
  prompts, and notifications always pass straight through.
- Caching error responses. Only successful (`result`, no `error`) responses
  are ever stored — caching a failure could mask a transient upstream problem
  as a permanent one.

These are deliberate v1 cuts, not oversights — anything on this list is a
`# ponytail:` upgrade path if a real need shows up later, not a missing
feature.

## Architecture

```
Agent / MCP client
        |
        v
+----------------------+
|   toolcall-cache      |  <- classifies traffic, decides cacheability,
|   (MCP proxy)          |     builds the key, reads/writes SQLite
+----------------------+
        |
   +----+----+
   v         v
 stdio      HTTP
   |         |
   v         v
upstream   upstream
MCP srv    MCP srv
```

The proxy is transport-agnostic at its core: `policy.py` and `key.py` decide
*whether* and *how* to cache; `proxy.py` classifies JSON-RPC messages and
assembles cache hits/stores; the two transports (`transports/stdio.py`,
`transports/http.py`) just plug different wire formats into that shared core.

- **stdio** (`StdioTransport`): spawns the upstream MCP server as a
  subprocess and pipes JSON-RPC lines between the agent's stdin/stdout and
  the subprocess's stdin/stdout, using asyncio queues so cache lookups never
  block the pass-through path for non-tool traffic.
- **HTTP** (`HttpProxyHandler` / `ThreadingHTTPServer`): a small reverse
  proxy — inspects each `POST` body for a `tools/call`, forwards `GET`s and
  cache-miss `POST`s to the configured upstream base URL via
  `urllib.request`, and copies headers through (stripping hop-by-hop
  headers).

Both transports track `tools/list` responses in memory to learn per-tool
`annotations.cacheable` declarations, and track in-flight `tools/call`
request IDs (`pending_calls`) so the matching response can be evaluated for
caching when it arrives.

### Module map

| Module | Responsibility |
|---|---|
| `cache.py` | SQLite schema, `get`/`put`/`clear`/`invalidate_tool`/`prune_expired`/`list_entries`/`stats`. |
| `key.py` | Canonical JSON serialization (recursively sorted dict keys) + SHA256 key derivation. |
| `policy.py` | Allowlist / denylist / annotation cacheability decision. Pure function, no I/O. |
| `proxy.py` | JSON-RPC message classification (`is_tools_call_request`, `is_tools_list_response`) and cache-hit/store assembly — the shared core both transports call into. |
| `transports/stdio.py` | Stdio proxy loop (asyncio, subprocess). |
| `transports/http.py` | HTTP reverse proxy (`http.server` + `urllib.request`, stdlib only). |
| `server.py` | CLI-args → transport wiring (`start` subcommand). |
| `cli.py` | `toolcall-cache` CLI: `start`, `clear`, `invalidate`, `list`, `stats`. |
| `selfcheck.py` | End-to-end test against a fake MCP server, plus a TTL-expiry unit check. |

## Locked design decisions

### Cache key: `sha256(canonical_json({server_id, tool_name, args}))`

**Why:** Two different upstream MCP servers can both expose a tool named
`read_file` with completely different semantics (or completely different
data). Hashing `tool_name` and `args` alone would let a cache entry from one
server answer a call meant for another. Including `server_id` in the key
payload eliminates that collision by construction — it's a config-time
identity (`--server-id`, default `default`), not something the proxy has to
infer.

Canonicalization (`key.py: _canonical`) recursively sorts dict keys before
serializing so that MCP clients which happen to serialize `arguments` keys in
a different order still hash to the same key. Lists keep their given order —
MCP tool arguments are JSON objects (named parameters), not positional
arrays, so list order inside an argument value is semantically meaningful and
must not be reordered.

### Default policy: allowlist-first, denylist always vetoes

**Why:** A cache is only safe if a misconfiguration fails toward "call the
tool again" rather than "return a stale or wrong answer silently." Allowlist-
first makes the *safe* mistake the default: forgetting to allowlist a tool
means it never caches (a miss, always correct, just slower). Denylist-first
(cache everything except what's excluded) makes the *unsafe* mistake the
default: forgetting to denylist a new mutating tool means it gets cached and
returns a stale side-effect-free response for what should have been a live
write.

Concretely (`policy.is_cacheable`), a tool is cached only if:
1. It does **not** match any denylist glob pattern (checked first — denylist
   always wins, even over an explicit allowlist entry), **and**
2. Either it is named in `--allowlist`, **or** the upstream server's
   `tools/list` response declared `annotations.cacheable: true` for it.

Default denylist patterns (`policy.DEFAULT_DENYLIST`, glob-matched via
`fnmatch`): `*_write*`, `send*`, `delete*`, `random*`, `time*`, `now*`,
`date*`. Configurable via `--denylist`.

### Tool annotation: `annotations.cacheable == true`

**Why:** Not every deployment controls the agent's `--allowlist` flag, and
some MCP servers already know which of their own tools are pure/idempotent.
Letting a server self-declare cacheability through the standard MCP
`annotations` field on a `tools/list` entry means well-behaved servers can
opt their own tools in without the operator having to hand-maintain an
allowlist. It's additive to the allowlist, not a replacement — the denylist
still overrides it.

### Storage: one SQLite file

**Why:** No server process to run, no external dependency, no telemetry —
consistent with the project's "local, offline, stdlib-only" constraint.
`cache.init_db` opens the DB in WAL mode with `synchronous=NORMAL`, which is
enough concurrency headroom for a single proxy process serving one agent
session without needing a client/server database.

Schema (`cache.py: SCHEMA_SQL`): one `cache_entries` table keyed by
`key_hash` (the SHA256 above), storing `server_id`, `tool_name`, `args_hash`
(a separate hash of just the arguments, kept for debugging/`list`/`stats`
readability), `result_json`, `created_at`, `expires_at`, and `hit_count`.
Indexes on `tool_name` and `expires_at` support `invalidate_tool` and TTL
pruning without a full table scan.

### TTL: per-entry, default 1 hour

**Why:** Even a deterministic-in-principle tool (a file read, a directory
listing) can go stale within a long session — files get edited, directories
get new entries. A TTL bounds how long a cache entry can silently diverge
from reality without requiring the operator to manually invalidate anything.
`--ttl <seconds>` on `start` sets it per proxy run; `cache.get` treats an
expired entry as a miss (not a delete-on-read — `prune_expired` and the
`INSERT ... ON CONFLICT` upsert in `cache.put` are what actually reclaim
expired rows, keeping the hot-path `get` a single indexed `SELECT`).

### Transports: stdio + HTTP, both stdlib-only

**Why:** These are the two MCP transports in real-world use — stdio for
locally-spawned servers (the common case for filesystem/shell/git-style
tools), Streamable HTTP for remotely-hosted or long-lived servers. Both are
implemented with only the Python standard library
(`asyncio`/`subprocess` for stdio; `http.server`/`urllib.request` for HTTP)
to keep the project dependency-free, matching the "stdlib + `sqlite3`" hard
constraint. Non-tool-call traffic (`initialize`, `notifications/*`,
`resources/*`, etc.) is forwarded through untouched on both transports — the
proxy only ever intercepts `tools/call` requests and their matching
responses.

## Scope / YAGNI

**In (v1, shipped):**
- MCP stdio proxy (subprocess spawn + bidirectional JSON-RPC line piping).
- MCP HTTP reverse proxy (`POST`/`GET`/`OPTIONS`, header pass-through minus
  hop-by-hop headers).
- SQLite content-addressed cache with TTL.
- Allowlist + denylist + MCP-annotation cacheability policy, denylist always
  wins.
- `toolcall-cache start|clear|invalidate|list|stats` CLI.
- `selfcheck.py`: fake MCP server with a cacheable `read_file` (annotation-
  declared) and a non-cacheable `now`; asserts the second `read_file` call is
  a cache hit (upstream counter stays at 1), every `now` call reaches
  upstream (counter increments each time), and TTL expiry flips a hit to a
  miss using the cache module's injectable `now` parameter (no real
  sleeping in tests).
- `benchmarks/bench_latency.py`: micro (raw `cache.get`/`cache.put` latency)
  and end-to-end (N repeated `read_file` calls through the live proxy vs. N
  `now` calls) measurements, feeding `docs/diagrams/generate.py`'s rendered
  README charts.

**Out (deliberately deferred, see Non-goals):** semantic/embedding caching,
cross-instance shared locking, a web UI, automatic cacheability inference,
caching non-`tools/call` traffic, caching error responses.

## Testing strategy

`selfcheck.py` is the only test surface (no test framework, per the
project's ponytail/stdlib-only stance) and runs the stdio proxy end-to-end
against a fake MCP server subprocess rather than mocking `cache`/`policy` in
isolation — the thing worth proving is that the wiring between transport,
policy, and cache actually caches what it should and forwards what it
shouldn't, not that each module's unit logic is individually correct (those
are simple enough to read directly). TTL expiry is checked via the cache
module's injectable `now: float | None` parameter on `get`/`put`, so the
test asserts real expiry behavior without a real `time.sleep`.

## Dependencies

- Runtime: none — Python 3.10+ stdlib only (`sqlite3`, `asyncio`,
  `http.server`, `urllib.request`, `argparse`, `json`, `hashlib`, `fnmatch`).
- Build: `setuptools` via `pyproject.toml`; `pipx install .`.

## License

MIT.
