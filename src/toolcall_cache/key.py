"""Content-addressed cache keys."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical(obj: Any) -> Any:
    """Recursively sort dict keys so the same data always serializes identically.

    # ponytail: lists keep order (MCP args are positional-by-name in JSON
    # objects, not arrays), but dict keys are sorted to make the key stable.
    """
    if isinstance(obj, dict):
        return {k: _canonical(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_canonical(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> str:
    """Return a compact, deterministic JSON representation."""
    return json.dumps(
        _canonical(obj),
        separators=(",", ":"),
        ensure_ascii=False,
        sort_keys=False,  # already sorted recursively
    )


def make_key(server_id: str, tool_name: str, args: dict[str, Any]) -> str:
    """Build a SHA256 content-addressed key for a tool call."""
    payload = {
        "server_id": server_id,
        "tool_name": tool_name,
        "args": args,
    }
    encoded = canonical_json(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_args_hash(args: dict[str, Any]) -> str:
    """Separate args hash for debugging and stats."""
    return hashlib.sha256(canonical_json(args).encode("utf-8")).hexdigest()
