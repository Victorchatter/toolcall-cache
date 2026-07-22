"""JSON-RPC MCP message classification and cache-hit assembly."""

from __future__ import annotations

from typing import Any

from . import cache, key, policy


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


def make_cached_response(request: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC response from a cached result."""
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
) -> dict[str, Any] | None:
    """If the call is cacheable and we have a hit, return the cached response."""
    if not policy.is_cacheable(tool_name, annotations.get(tool_name), allowlist, denylist):
        return None
    key_hash = key.make_key(server_id, tool_name, arguments)
    hit = cache.get(conn, key_hash)
    if hit is None:
        return None
    return make_cached_response(request, hit["result"])


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
) -> None:
    """Store a successful response in the cache if the tool is cacheable."""
    if not policy.is_cacheable(tool_name, annotations.get(tool_name), allowlist, denylist):
        return
    should_cache, result = should_cache_response(response)
    if not should_cache:
        return
    key_hash = key.make_key(server_id, tool_name, arguments)
    args_hash = key.make_args_hash(arguments)
    cache.put(conn, key_hash, server_id, tool_name, args_hash, result, ttl)
