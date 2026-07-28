"""JSON-RPC MCP message classification and cache-hit assembly."""

from __future__ import annotations

from typing import Any

from . import cache, key, policy


class FuzzyConfig:
    """Configuration for semantic/fuzzy cache matching."""

    def __init__(
        self,
        enabled: bool = False,
        ignore_keys: set[str] | frozenset[str] | None = None,
        threshold: float = 0.85,
        window: int = 100,
    ) -> None:
        self.enabled = enabled
        self.ignore_keys: frozenset[str] = frozenset(ignore_keys) if ignore_keys else frozenset()
        self.threshold = float(threshold)
        self.window = int(window)


def is_tools_call_request(msg: dict[str, Any]) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Detect a ``tools/call`` JSON-RPC request.

    Returns (is_call, tool_name, arguments).
    """
    if not isinstance(msg, dict):
        return False, None, None
    if msg.get("jsonrpc") != "2.0":
        return False, None, None
    if msg.get("method") != "tools/call":
        return False, None, None
    params = msg.get("params")
    if not isinstance(params, dict):
        return False, None, None
    tool_name = params.get("name")
    if not isinstance(tool_name, str):
        return False, None, None
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    return True, tool_name, arguments


def is_tools_list_response(msg: dict[str, Any]) -> bool:
    """Detect a successful ``tools/list`` JSON-RPC response."""
    if not isinstance(msg, dict):
        return False
    if msg.get("jsonrpc") != "2.0":
        return False
    if "id" not in msg:
        return False
    result = msg.get("result")
    return isinstance(result, dict) and "tools" in result


def extract_tool_annotations(msg: dict[str, Any]) -> dict[str, dict]:
    """Extract annotation dicts from a ``tools/list`` response."""
    annotations: dict[str, dict] = {}
    result = msg.get("result", {})
    tools = result.get("tools", [])
    if not isinstance(tools, list):
        return annotations
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            continue
        ann = tool.get("annotations")
        if isinstance(ann, dict):
            annotations[name] = ann
    return annotations


def _with_fuzzy_meta(result: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``result`` with the fuzzy-match meta flag set."""
    if not isinstance(result, dict):
        return result
    merged = dict(result)
    meta = merged.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
        merged["_meta"] = meta
    meta["locallab_fuzzy_match"] = True
    return merged


def make_cached_response(request: dict[str, Any], result: dict[str, Any], fuzzy: bool = False) -> dict[str, Any]:
    """Build a JSON-RPC response from a cached result."""
    if fuzzy:
        result = _with_fuzzy_meta(result)
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "result": result,
    }


def should_cache_response(response: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    """Return (should_cache, result) for a JSON-RPC response.

    We only cache successful ``tools/call`` responses (i.e., responses with a
    result, not an error). # ponytail: caching errors could mask transient
    failures, so v1 keeps it simple and only caches successful results.
    """
    if not isinstance(response, dict):
        return False, None
    if response.get("jsonrpc") != "2.0":
        return False, None
    if "error" in response:
        return False, None
    result = response.get("result")
    if not isinstance(result, dict):
        return False, None
    return True, result


def try_cache_hit(
    conn,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    request: dict[str, Any],
    annotations: dict[str, dict],
    allowlist: list[str],
    denylist: list[str],
    fuzzy_config: FuzzyConfig | None = None,
) -> dict[str, Any] | None:
    """If the call is cacheable and we have a hit, return the cached response."""
    if not policy.is_cacheable(tool_name, annotations.get(tool_name), allowlist, denylist):
        return None

    fuzzy_config = fuzzy_config or FuzzyConfig()

    # 1. Exact-key lookup (using the same normalization that storage uses).
    key_hash = key.make_key(
        server_id,
        tool_name,
        arguments,
        fuzzy=fuzzy_config.enabled,
        ignore_keys=fuzzy_config.ignore_keys,
    )
    hit = cache.get(conn, key_hash)
    if hit is not None:
        return make_cached_response(request, hit["result"], fuzzy=False)

    # 2. Fuzzy lookup when enabled and exact miss.
    if fuzzy_config.enabled:
        fuzzy_hit = cache.fuzzy_lookup(
            conn,
            server_id,
            tool_name,
            arguments,
            threshold=fuzzy_config.threshold,
            window=fuzzy_config.window,
            ignore_keys=fuzzy_config.ignore_keys,
        )
        if fuzzy_hit is not None:
            return make_cached_response(request, fuzzy_hit["result"], fuzzy=True)

    return None


def store_response(
    conn,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    response: dict[str, Any],
    annotations: dict[str, dict],
    allowlist: list[str],
    denylist: list[str],
    ttl: float,
    fuzzy_config: FuzzyConfig | None = None,
) -> None:
    """Store a successful response in the cache if the tool is cacheable."""
    if not policy.is_cacheable(tool_name, annotations.get(tool_name), allowlist, denylist):
        return
    should_cache, result = should_cache_response(response)
    if not should_cache:
        return

    fuzzy_config = fuzzy_config or FuzzyConfig()
    fuzzy = fuzzy_config.enabled

    key_hash = key.make_key(server_id, tool_name, arguments, fuzzy=fuzzy, ignore_keys=fuzzy_config.ignore_keys)
    args_hash = key.make_args_hash(arguments, fuzzy=fuzzy, ignore_keys=fuzzy_config.ignore_keys)
    normalized_args_json = key.make_normalized_json(arguments, fuzzy_config.ignore_keys)
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
