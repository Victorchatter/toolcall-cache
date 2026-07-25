---
id: "4017a3b5"
type: context
tags: []
created: "2026-07-25T13:27:51.338Z"
source: manual
---
toolcall-cache: local, content-addressed cache for MCP tool results — sits as a proxy, caches deterministic tool calls, skips ones the user hasn't allowlisted. CLI entry point: toolcall-cache (src/toolcall_cache/cli.py). v0.1.0 shipped 2026-07-22. # ponytail: cache key assumes MCP args stay ordered as given; cacheability policy uses simple glob matching, not a full rules engine.
