# Changelog

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
