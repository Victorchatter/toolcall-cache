<p align="center">
  <img src="docs/diagrams/banner.svg" alt="toolcall-cache" width="800">
</p>

<p align="center">
  <a href="https://github.com/Victorchatter/toolcall-cache/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
  </a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/dependencies-stdlib%20only-success.svg" alt="stdlib only">
</p>

# toolcall-cache

**A local, content-addressed cache for MCP tool results.**

Drop it between any MCP-using agent and any MCP server. For deterministic tools — file reads, greps, GETs, listings — repeated calls with the same arguments return the cached result instead of hitting the upstream server again. Less spend, lower latency, and zero external dependencies.

---

## What problem this solves

Long agent sessions repeat the same tool calls over and over:

- Re-reading the same `README.md` on every planning loop.
- Re-grepping the same symbol across a codebase.
- Re-fetching the same HTTP endpoint for context.

Each call costs time, tokens, and (for paid MCP servers or API-backed tools) money.

`toolcall-cache` is a framework-agnostic cache layer that works across Claude Code, Codex, Cursor, or any other MCP client — see [How this compares](#how-this-compares) for what specifically sets it apart from the other MCP caching proxies out there.

![Cache impact](docs/diagrams/cache-impact.svg)

### Without the cache

```mermaid
sequenceDiagram
    participant Agent
    participant Upstream as MCP server
    Agent->>Upstream: tools/call read_file(path="README.md")
    Upstream-->>Agent: content
    Agent->>Upstream: tools/call read_file(path="README.md")
    Upstream-->>Agent: content
    Agent->>Upstream: tools/call read_file(path="README.md")
    Upstream-->>Agent: content
```

### With toolcall-cache

```mermaid
sequenceDiagram
    participant Agent
    participant Proxy as toolcall-cache
    participant SQLite as SQLite cache
    participant Upstream as MCP server
    Agent->>Proxy: tools/call read_file(path="README.md")
    Proxy->>SQLite: key miss?
    SQLite-->>Proxy: miss
    Proxy->>Upstream: forward call
    Upstream-->>Proxy: content
    Proxy->>SQLite: store result
    Proxy-->>Agent: content
    Agent->>Proxy: tools/call read_file(path="README.md")
    SQLite-->>Proxy: hit
    Proxy-->>Agent: cached content
```

![Latency comparison](docs/diagrams/latency.svg)

---

## How this compares

MCP caching proxies are a crowded and growing category. The closest projects:

- [swapnilsurdi/mcp-cache](https://github.com/swapnilsurdi/mcp-cache) — a stdio-only proxy that caches responses over 900KB by unique ID with a 1-hour TTL. No content-addressed key and no allow/deny policy.
- [cmaurer/mcp-cache](https://github.com/cmaurer/mcp-cache) — owns the `mcp-cache` name on PyPI, but it's a library MCP servers import internally, not a transparent proxy you drop in front of one.
- [kira-autonoma/mcp-context-proxy](https://github.com/kira-autonoma/mcp-context-proxy) — caches schema lazy-loading, a different problem from caching tool-call results.

`toolcall-cache` differs on four specifics: cache keys are `sha256(canonical_json({server_id, tool, args}))` — content-addressed, not size- or ID-based; the policy is default-safe, with a denylist that always vetoes the allowlist; it speaks both stdio and HTTP transports, not just one; and it ships `invalidate` / `list` / `stats` CLI commands alongside TTL expiry.

---

## How it works

`toolcall-cache` runs as a transparent MCP proxy. Your agent's MCP config points at the cache, and the cache points at the real MCP server.

![Architecture](docs/diagrams/architecture.svg)

1. **Agent sends** a `tools/call` request.
2. **Proxy checks** whether the tool is cacheable and whether a fresh entry exists.
3. **Cache hit:** the proxy returns the stored result directly — no upstream traffic.
4. **Cache miss:** the proxy forwards the call, stores the successful response, and returns it.
5. Non-tool traffic (`initialize`, `tools/list`, notifications, etc.) passes through untouched.

Cache key:

```
sha256(canonical_json({server_id, tool_name, arguments}))
```

The `server_id` prevents collisions between tools with the same name on different upstream servers.

### Fuzzy / semantic cache

Enable fuzzy mode with `--fuzzy` to treat small argument variations as cache hits. The proxy still tries an exact key lookup first; only on an exact miss does it scan the most recent entries for the same tool and pick the closest normalized match above `--fuzzy-threshold`.

Normalization rules:

- String values are stripped of surrounding whitespace and lowercased.
- Dict keys are sorted recursively so key order does not matter.
- Keys listed in `--fuzzy-ignore-keys` are dropped at every dict level.
- Numbers, booleans, lists, and `null` are left unchanged.

A fuzzy hit is returned with a marker so clients can tell the difference:

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "result": {
    "content": [...],
    "_meta": {"locallab_fuzzy_match": true}
  }
}
```

---

## Methodology: what to use it for

Use `toolcall-cache` for **deterministic, read-only tools** where the answer does not change between calls in the same session:

| Good fit | Why |
|----------|-----|
| `read_file` | File contents are stable for the lifetime of the cache. |
| `grep`, `search` | Re-searching the same query returns the same hits. |
| `list_directory` | Directory listings change rarely during a session. |
| `http_get` | Safe for idempotent GET endpoints. |
| `git_status`, `git_diff` | Repeated status checks are common in coding agents. |

**Do not cache** tools that mutate state, return time-sensitive data, or produce random output:

| Avoid | Why |
|-------|-----|
| `write_file`, `send_email` | Side effects must run every time. |
| `now`, `date`, `time` | Values change continuously. |
| `random*` | Non-deterministic by definition. |
| `delete*` | Destructive operations must reach the server. |

The default denylist (`*_write*`, `send*`, `delete*`, `random*`, `time*`, `now*`, `date*`) blocks these automatically. You can override it, but the denylist always wins over the allowlist for safety.

---

## Installation

```bash
pipx install .
```

Or install from source:

```bash
git clone https://github.com/Victorchatter/toolcall-cache.git
cd toolcall-cache
pip install -e .
```

No external dependencies — just Python 3.10+ and the stdlib.

---

## Quick start

### Stdio MCP server

Point your agent at the proxy instead of the upstream server:

```bash
toolcall-cache start \
  --transport stdio \
  --upstream "npx -y @modelcontextprotocol/server-filesystem /path/to/project" \
  --allowlist read_file,read,grep,list_directory \
  --ttl 3600
```

Then configure your MCP client to use this command as its server.

### HTTP MCP server

```bash
toolcall-cache start \
  --transport http \
  --upstream http://localhost:3001 \
  --allowlist read_file,search,http_get \
  --port 8787
```

Your agent connects to `http://localhost:8787`; the proxy forwards to `http://localhost:3001`.

### CLI management

```bash
# Show cache statistics
toolcall-cache stats

# Live updating stats table (refreshes every 2 seconds)
toolcall-cache stats --watch

# Live updating stats with a custom interval
toolcall-cache stats --watch 5

# Watch mode without clearing the screen, for CI/logging
toolcall-cache stats --watch --no-clear

# List cached entries
toolcall-cache list

# Invalidate all cached results for one tool
toolcall-cache invalidate read_file

# Clear everything
toolcall-cache clear

# Preview whether two argument sets would fuzzy-match
toolcall-cache fuzzy-test grep '{"pattern":"TODO","path":"src"}' '{"pattern":"todo","path":"src"}'
# tool: grep
# threshold: 0.85
# similarity: 1.00
# match: yes
```

---

## Example: stop paying for the same grep twice

Imagine a coding agent working on a large repo. It keeps asking:

```json
{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"grep","arguments":{"pattern":"TODO","path":"src"}}}
```

Each call scans the filesystem and returns the same 50 matches. Over a 30-minute session the agent issues this query 12 times.

**Without `toolcall-cache`:** 12 upstream scans.

**With `toolcall-cache`:** 1 upstream scan + 11 instant cache hits.

Start the proxy with `grep` allowlisted:

```bash
toolcall-cache start \
  --transport stdio \
  --upstream "python my_mcp_server.py" \
  --allowlist grep,read_file \
  --ttl 1800
```

The first `grep` runs normally. Every identical repeat returns in microseconds from the local SQLite cache.

---

## Cache policy

The default policy is **allowlist-first and denylist-vetoes**:

- A tool is cached only if it is **explicitly allowlisted** (`--allowlist`) **or** its MCP `annotations` declare `cacheable: true`.
- The **denylist** (`--denylist`) overrides everything. Default denylist:
  - `*_write*`
  - `send*`
  - `delete*`
  - `random*`
  - `time*`
  - `now*`
  - `date*`

This design makes the safe choice the default: a configuration mistake costs you a cache miss, never a stale or wrong answer.

### Hydrating from a tape

If you recorded an agent run with `agent-vcr`, you can seed the cache so a
partial replay avoids live MCP calls:

```bash
toolcall-cache hydrate --tape run.jsonl --server-id fs
```

`hydrate` parses the tape and stores every successful `tools/call` result,
obeying the same allowlist/denylist policy and hash-key logic as live proxy
mode. Use `--dry-run` to preview what would be cached.

### MCP annotation example

If your MCP server advertises:

```json
{
  "name": "read_file",
  "annotations": {"cacheable": true}
}
```

`read_file` will be cached even without `--allowlist read_file`.

---

## CLI reference

```text
toolcall-cache --help
toolcall-cache start --help
```

| Command | Purpose |
|---------|---------|
| `start` | Run the MCP proxy. |
| `clear` | Delete every cached entry. |
| `invalidate <tool>` | Delete entries for a single tool name. |
| `list` | Show cached entries. |
| `stats` | Show aggregate hits, misses, hit rate, entries, expiring entries, and per-tool stats. |
| `stats --watch [SECONDS]` | Poll and refresh the stats table live (default 2s). |
| `fuzzy-test <tool> <args-a> <args-b>` | Preview whether two argument sets would fuzzy-match. |
| `hydrate --tape <tape.jsonl>` | Pre-populate the cache from an `agent-vcr` tape. |

Common options:

| Flag | Default | Description |
|------|---------|-------------|
| `--db` | *(resolved)* | SQLite cache file (overrides `--state-dir`). |
| `--state-dir` | `~/.locallab` | Unified LocalLab state directory. |
| `--allowlist` | *(empty)* | Comma-separated tool names to cache. |
| `--denylist` | `*_write*,send*,delete*,random*,time*,now*,date*` | Glob patterns never cached. |
| `--ttl` | `3600` | Cache TTL in seconds. |
| `--server-id` | `default` | Server identity in cache keys. |
| `--fuzzy` | off | Enable fuzzy matching after an exact-key miss. |
| `--fuzzy-ignore-keys` | *(empty)* | Argument keys to drop recursively before fuzzy comparison. |
| `--fuzzy-threshold` | `0.85` | Minimum Levenshtein similarity for a fuzzy hit. |
| `--fuzzy-window` | `100` | Maximum recent entries scanned per fuzzy lookup. |

By default `toolcall-cache` stores its SQLite database in the unified LocalLab
state directory at `~/.locallab/toolcall-cache/cache.db`. If `~/.locallab` cannot
be created, it falls back to the legacy path `~/.toolcall-cache/toolcall-cache.db`.
Use `--db` to override either default with an explicit path.

---

## Development & testing

Run the built-in self-test:

```bash
python selfcheck.py
```

It spawns a fake MCP server with a cacheable `read_file` and a non-cacheable `now`, then asserts:

- The second `read_file` call hits the cache (upstream counter stays at 1).
- Every `now` call reaches upstream (counter increments).
- A cached entry returns a hit before its TTL expires and a miss after
  (checked via the cache module's injectable `now`, so no real sleeping).
- Fuzzy mode matches calls that differ only by whitespace/case or by an ignored key.
- The `fuzzy-test` CLI previews whether two argument sets would fuzzy-match.

### Benchmarks

```bash
python benchmarks/bench_latency.py
```

Measures two things and writes `benchmarks/results.json`:

- **micro** — pure `cache.get` (hit) and `cache.put` SQLite latency in µs.
- **e2e** — N repeated `read_file` calls through the stdio proxy (cached after
  the first) vs N `now` calls (never cached, always forwarded to the upstream
  subprocess), in ms/call. A real cached-vs-uncached comparison through the
  actual proxy.

`docs/diagrams/generate.py` reads `results.json` to render `latency.svg` from
the measured cache-hit latency (falling back to an illustrative sub-millisecond
default before the benchmark has been run). The upstream bar is a *typical*
network-tool round-trip, not measured — a real upstream's cost varies by tool,
so the README and the chart caption label it as typical. Re-run the benchmark
and then `python docs/diagrams/generate.py` to refresh the chart with new
numbers.

Regenerate README diagrams:

```bash
python docs/diagrams/generate.py
```

---

## Project structure

```
toolcall-cache/
├── src/toolcall_cache/
│   ├── cache.py           # SQLite storage
│   ├── key.py             # Canonical JSON + SHA256 keys
│   ├── policy.py          # Allowlist / denylist / annotations
│   ├── proxy.py           # MCP message classification
│   ├── cli.py             # toolcall-cache command
│   ├── server.py          # Transport wiring
│   └── transports/
│       ├── stdio.py       # Stdio MCP proxy
│       └── http.py        # HTTP MCP reverse proxy
├── selfcheck.py           # End-to-end test (+ TTL expiry unit check)
├── benchmarks/            # bench_latency.py + measured results.json
├── docs/diagrams/         # Generated SVGs (read benchmarks/results.json)
├── pyproject.toml
├── LICENSE
└── README.md
```

---

## License

MIT. See [LICENSE](LICENSE).
