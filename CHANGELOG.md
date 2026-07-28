# Changelog

## 0.3.0

### Added
- Semantic / fuzzy cache mode (`--fuzzy`).
  - `--fuzzy-ignore-keys` drops specified argument keys recursively before
    comparison and key hashing.
  - `--fuzzy-threshold` sets the minimum Levenshtein similarity (default
    `0.85`) for a fuzzy hit.
  - `--fuzzy-window` limits the scan to the last N entries for the same tool
    (default `100`).
  - Normalization rules: string values are stripped and lowercased; dict keys
    are sorted recursively; ignored keys are omitted.
  - Exact-key lookup is tried first; fuzzy lookup is only used on an exact miss.
  - Fuzzy hits inject `"_meta": {"locallab_fuzzy_match": true}` into the
    JSON-RPC response.
- `toolcall-cache fuzzy-test` CLI subcommand for quick offline validation of
  fuzzy matching behavior and exit codes.
- Schema migration adds `normalized_args` and `tool_signature` columns to the
  SQLite `cache_entries` table.

## 0.2.0

### Added
- `toolcall-cache stats --watch [SECONDS]` polls the SQLite store and prints a
  live updating table (default interval: 2 seconds).
  - New columns: total entries, hits, misses, hit rate %, entries expiring in
    5 minutes, and top 5 cached tools by hit count.
  - ANSI screen clearing between updates; `--no-clear` for CI/logging.
  - Clean `Ctrl+C` exit.
- `selfcheck.py` verifies `stats` reports `hits >= 1` after two identical cached calls.

## 0.1.0

### Added
- Initial release: local, content-addressed cache for MCP tool results with
  stdio and HTTP transports, allowlist/denylist policy, and TTL expiry.
