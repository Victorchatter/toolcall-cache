"""Content-addressed cache keys with optional fuzzy normalization."""

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


def levenshtein(a: str, b: str) -> int:
    """Return the deterministic Levenshtein edit distance between two strings."""
    # Use the shorter string as the inner dimension to keep memory bounded.
    if len(a) < len(b):
        a, b = b, a

    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ach in enumerate(a, start=1):
        current = [i]
        for j, bch in enumerate(b, start=1):
            cost = 0 if ach == bch else 1
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


def levenshtein_ratio(a: str, b: str) -> float:
    """Return a similarity ratio in [0.0, 1.0] based on Levenshtein distance.

    Mirrors the ``SequenceMatcher.ratio`` formula:
    ``(sum_lengths - distance) / sum_lengths``.
    """
    total = len(a) + len(b)
    if total == 0:
        return 1.0
    return (total - levenshtein(a, b)) / total


def _normalize_value(value: Any, ignore_keys: set[str] | frozenset[str]) -> Any:
    """Recursively normalize a single value for fuzzy comparison."""
    if isinstance(value, dict):
        return {
            k: _normalize_value(v, ignore_keys)
            for k, v in sorted(value.items())
            if k not in ignore_keys
        }
    if isinstance(value, list):
        return [_normalize_value(v, ignore_keys) for v in value]
    if isinstance(value, str):
        return value.strip().lower()
    return value


def normalize_args(args: dict[str, Any], ignore_keys: set[str] | frozenset[str] | None = None) -> dict[str, Any]:
    """Return a normalized copy of ``args`` for fuzzy matching.

    Normalization rules:
    - dict keys are sorted recursively;
    - string values are stripped of surrounding whitespace and lowercased;
    - keys present in ``ignore_keys`` are dropped at every dict level;
    - lists, numbers, booleans, and ``None`` are left as-is.
    """
    if ignore_keys is None:
        ignore_keys = frozenset()
    else:
        ignore_keys = frozenset(ignore_keys)
    return _normalize_value(args, ignore_keys)


def make_key(
    server_id: str,
    tool_name: str,
    args: dict[str, Any],
    fuzzy: bool = False,
    ignore_keys: set[str] | frozenset[str] | None = None,
) -> str:
    """Build a SHA256 content-addressed key for a tool call.

    When ``fuzzy`` is enabled, arguments are normalized before hashing so that
    minor variations (whitespace, case, ignored keys) map to the same key.
    """
    payload: dict[str, Any] = {
        "server_id": server_id,
        "tool_name": tool_name,
        "args": normalize_args(args, ignore_keys) if fuzzy else args,
    }
    encoded = canonical_json(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_args_hash(
    args: dict[str, Any],
    fuzzy: bool = False,
    ignore_keys: set[str] | frozenset[str] | None = None,
) -> str:
    """Separate args hash for debugging and stats.

    When ``fuzzy`` is enabled, the hash is computed over normalized arguments.
    """
    normalized = normalize_args(args, ignore_keys) if fuzzy else args
    return hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()


def make_normalized_json(args: dict[str, Any], ignore_keys: set[str] | frozenset[str] | None = None) -> str:
    """Return the canonical JSON form used for fuzzy comparison."""
    return canonical_json(normalize_args(args, ignore_keys))
