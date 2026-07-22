# toolcall-cache — bootstrap session prompt

You are bootstrapping a new open-source project. Follow the full process: `superpowers:brainstorming` → lock design → write spec to `docs/superpowers/specs/YYYY-MM-DD-toolcall-cache-design.md` → commit → `superpowers:writing-plans` (approve) → implement via `superpowers:executing-plans`. Verify with `selfcheck.py` before done.

## Idea (one-liner)
A local, content-addressed cache for agent tool results. Sits as a tiny MCP proxy between the agent and any MCP server. For deterministic tools (file reads, greps, GETs), it returns the cached result for a repeated `(tool, args)` call instead of re-running the tool — slashing repeat spend and latency in long sessions. Cache key = `sha256(canonical_json(tool_name, args))`. One SQLite store.

## Why it doesn't exist
Per-framework caches exist, but nothing framework-agnostic works for *any* MCP-using agent. As MCP becomes the standard tool layer, a single shared cache is the obvious missing primitive.

## Hard constraints
- Python, `pipx install .`. Fully local/offline: SQLite cache, no telemetry.
- Sits as an MCP proxy (stdio + Streamable HTTP transports). Agent's MCP config points at toolcall-cache, which forwards to the real MCP server upstream.
- Cacheability policy: not every tool is cacheable (a clock, a random generator, a "send email" must not be cached). Ship a denylist approach + an opt-in allowlist; default: cache only tools explicitly marked cacheable by name or by an MCP tool annotation when present. Never cache tools whose name matches a denylist (`*_write*`, `send*`, `delete*`, `random*`, `time*`, `now*`, `date*`) — document and make configurable.
- Cache invalidation: `--ttl` per cache entry (default 1h), `toolcall-cache clear`, `toolcall-cache invalidate <tool>`.
- CLI to start the proxy + a small `toolcall-cache` management CLI for clear/list/stats.
- Small and sharp. Ponytail: stdlib + `sqlite3`, no unrequested abstractions. `# ponytail:` comments on simplifications.
- One `selfcheck.py`: fake MCP server with a cacheable `read_file` and a non-cacheable `now`; assert the second `read_file` with same args returns cached (upstream call counter stays at 1) and `now` is never cached (counter increments each call).
- License MIT. README with a "stop paying for the same grep twice" example.

## Scope / YAGNI (v1)
Ship: MCP stdio + HTTP proxy, SQLite content-addressed cache, allowlist/denylist policy, TTL, clear/invalidate/stats CLI. Out: semantic caching (embeddings), multi-agent shared locking, web UI, automatic cacheability detection.

## Inputs to lock during brainstorming
- Cache key: include server identity? (recommend yes → `(server, tool, args_hash)` so same-named tools on different servers don't collide.)
- Default policy: denylist-first (cache everything not denied) vs allowlist-first (cache nothing unless marked). Recommend denylist-first *only* for tools explicitly marked cacheable, else allowlist — pick one and justify.
- HTTP transport caching scope.

One of 10 sibling local-first agent-tooling projects. Keep it small and ship it.