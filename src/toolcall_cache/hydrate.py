"""Hydrate the toolcall-cache SQLite store from an agent-vcr tape."""

from __future__ import annotations

import json
from typing import Any

from . import cache, key, policy, proxy


def _load_tape_events(path: str) -> list[dict[str, Any]]:
    """Read a JSONL tape and return the parsed events."""
    events: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"bad JSONL at {path}:{i}: {exc}") from exc
    return events


def _pair_tool_calls_and_results(events: list[dict[str, Any]]) -> list[tuple[dict, dict]]:
    """Pair tool_call events with their matching tool_result events by seq."""
    by_seq: dict[int, dict[str, dict]] = {}
    for ev in events:
        seq = ev.get("seq")
        if not isinstance(seq, int):
            continue
        bucket = by_seq.setdefault(seq, {})
        kind = ev.get("kind")
        if kind in ("tool_call", "tool_result"):
            bucket[kind] = ev

    pairs = []
    for seq in sorted(by_seq):
        bucket = by_seq[seq]
        if "tool_call" in bucket and "tool_result" in bucket:
            pairs.append((bucket["tool_call"], bucket["tool_result"]))
    return pairs


def _extract_result(result: Any) -> dict[str, Any] | None:
    """Return the result payload if it is cacheable, else None."""
    if not isinstance(result, dict):
        return None
    if "error" in result or result.get("is_error"):
        return None
    return result


def _is_cacheable(
    tool_name: str,
    allowlist: list[str],
    denylist: list[str],
) -> bool:
    """Check cacheability using the same policy as live proxy mode.

    No MCP annotations are available from a tape, so pass an empty annotation
    dict. This means cacheability is driven by allowlist/denylist only.
    """
    return policy.is_cacheable(tool_name, {}, allowlist, denylist)


def hydrate(
    conn,
    tape_path: str,
    *,
    server_id: str = "default",
    allowlist: list[str] | None = None,
    denylist: list[str] | None = None,
    ttl: float = 3600.0,
    fuzzy_config: proxy.FuzzyConfig | None = None,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Pre-populate the cache from tool results in a tape.

    Returns ``(cached_count, skipped_count)``.
    """
    allowlist = allowlist or []
    denylist = denylist or []
    fuzzy_config = fuzzy_config or proxy.FuzzyConfig()
    fuzzy = fuzzy_config.enabled

    events = _load_tape_events(tape_path)
    pairs = _pair_tool_calls_and_results(events)

    cached = 0
    skipped = 0
    for tool_call, tool_result in pairs:
        tool_name = tool_call.get("tool")
        if not isinstance(tool_name, str):
            skipped += 1
            continue

        if not _is_cacheable(tool_name, allowlist, denylist):
            skipped += 1
            continue

        arguments = tool_call.get("args", {})
        if not isinstance(arguments, dict):
            arguments = {}

        result = _extract_result(tool_result.get("result"))
        if result is None:
            skipped += 1
            continue

        key_hash = key.make_key(
            server_id,
            tool_name,
            arguments,
            fuzzy=fuzzy,
            ignore_keys=fuzzy_config.ignore_keys,
        )
        args_hash = key.make_args_hash(
            arguments,
            fuzzy=fuzzy,
            ignore_keys=fuzzy_config.ignore_keys,
        )
        normalized_args_json = key.make_normalized_json(
            arguments,
            fuzzy_config.ignore_keys,
        )

        if dry_run:
            print(
                f"would cache: {tool_name}({json.dumps(arguments, sort_keys=True)}) "
                f"-> key={key_hash[:16]}..."
            )
            cached += 1
            continue

        cache.put(
            conn,
            key_hash,
            server_id,
            tool_name,
            args_hash,
            result,
            ttl,
            normalized_args_json=normalized_args_json,
            tool_signature=tool_name,
        )
        cached += 1

    return cached, skipped
