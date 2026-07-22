"""Cacheability policy: allowlist + annotations, denylist always wins."""

from __future__ import annotations

import fnmatch
from typing import Iterable

# Default denylist from the spec. These patterns are never cached, even if the
# user explicitly allowlisted them. # ponytail: simple glob matching is enough
# for v1; no regex dependency needed.
DEFAULT_DENYLIST = [
    "*_write*",
    "send*",
    "delete*",
    "random*",
    "time*",
    "now*",
    "date*",
]


def _matches_any(name: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def parse_name_list(value: str | None) -> list[str]:
    """Parse a comma-separated list of tool names, ignoring empty items."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def is_cacheable(
    tool_name: str,
    annotations: dict | None,
    allowlist: list[str],
    denylist: list[str],
) -> bool:
    """Return True if the tool call may be cached.

    Policy order:
    1. Denylist always vetoes.
    2. Explicit allowlist approves.
    3. MCP annotation ``cacheable: true`` approves.
    4. Default is deny (allowlist-first mode).
    """
    if _matches_any(tool_name, denylist):
        return False
    if tool_name in allowlist:
        return True
    if annotations and annotations.get("cacheable") is True:
        return True
    return False
