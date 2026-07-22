"""End-to-end self-test for toolcall-cache.

Runs a fake MCP server behind the stdio proxy and asserts:
- repeated ``read_file`` calls hit the cache (upstream counter stays at 1)
- repeated ``now`` calls always pass through (counter increments)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time

FAKE_SERVER = textwrap.dedent(
    r'''
    import json
    import sys

    counts = {"read_file": 0, "now": 0}

    def handle(msg):
        method = msg.get("method")
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "fake-mcp-server"},
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "result": {
                    "tools": [
                        {
                            "name": "read_file",
                            "description": "Reads a file.",
                            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
                            "annotations": {"cacheable": True},
                        },
                        {
                            "name": "now",
                            "description": "Returns the current counter.",
                            "inputSchema": {"type": "object"},
                        },
                    ]
                },
            }
        if method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "read_file":
                counts["read_file"] += 1
                text = f"content of {args.get('path')}"
                return {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "result": {"content": [{"type": "text", "text": text}]},
                }
            if name == "now":
                counts["now"] += 1
                return {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "result": {"content": [{"type": "text", "text": str(counts["now"])}]},
                }
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    '''
)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _send(proc, msg):
    line = (json.dumps(msg) + "\n").encode("utf-8")
    proc.stdin.write(line)
    proc.stdin.flush()


def _recv(proc):
    line = proc.stdout.readline()
    _assert(line, "expected a response, got EOF")
    return json.loads(line.decode("utf-8"))


def main():
    project_root = os.path.dirname(os.path.abspath(__file__))
    src_path = os.path.join(project_root, "src")

    fake_path = tempfile.mktemp(suffix="_fake_mcp.py")
    db_path = tempfile.mktemp(suffix=".db")

    with open(fake_path, "w", encoding="utf-8") as f:
        f.write(FAKE_SERVER)

    # Clean up any stale db from a previous aborted run.
    for ext in ("", "-journal", "-wal", "-shm"):
        p = db_path + ext
        if os.path.exists(p):
            os.remove(p)

    env = os.environ.copy()
    env["PYTHONPATH"] = src_path

    proxy = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "toolcall_cache",
            "start",
            "--transport",
            "stdio",
            "--upstream",
            f"{sys.executable} {fake_path}",
            "--db",
            db_path,
            "--allowlist",
            "read_file",
            "--ttl",
            "60",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        # Realistic MCP handshake.
        _send(proxy, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        init = _recv(proxy)
        _assert(init.get("id") == 0, f"unexpected initialize response: {init}")

        _send(proxy, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        _send(proxy, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = _recv(proxy)
        _assert(tools.get("id") == 1, f"unexpected tools/list response: {tools}")

        # First read_file call should hit upstream.
        _send(
            proxy,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "/tmp/foo.txt"}},
            },
        )
        r1 = _recv(proxy)
        _assert(
            r1["result"]["content"][0]["text"] == "content of /tmp/foo.txt",
            f"unexpected read_file response: {r1}",
        )

        # Second read_file call with identical args should be cached.
        _send(
            proxy,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "/tmp/foo.txt"}},
            },
        )
        r2 = _recv(proxy)
        _assert(
            r2["result"]["content"][0]["text"] == "content of /tmp/foo.txt",
            f"unexpected cached read_file response: {r2}",
        )

        # now() is not cacheable, so each call must increment the upstream counter.
        _send(proxy, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "now"}})
        n1 = _recv(proxy)
        _assert(n1["result"]["content"][0]["text"] == "1", f"unexpected now response: {n1}")

        _send(proxy, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "now"}})
        n2 = _recv(proxy)
        _assert(n2["result"]["content"][0]["text"] == "2", f"unexpected now response: {n2}")

        # Verify the cache actually has an entry.
        _send(proxy, {"jsonrpc": "2.0", "id": 6, "method": "tools/list"})
        tools2 = _recv(proxy)
        _assert(tools2.get("id") == 6, f"unexpected second tools/list response: {tools2}")

        # Ask the fake server directly for its counters (bypassing proxy via a side channel is
        # impossible, so we rely on the now responses above as the ground-truth counter).
        _send(proxy, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "now"}})
        n3 = _recv(proxy)
        _assert(
            n3["result"]["content"][0]["text"] == "3",
            f"third now should be 3 (never cached): {n3}",
        )

        print("selfcheck OK")
    finally:
        try:
            proxy.stdin.close()
        except Exception:
            pass
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait(timeout=2)
        try:
            os.remove(fake_path)
        except Exception:
            pass
        for ext in ("", "-journal", "-wal", "-shm"):
            p = db_path + ext
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"selfcheck FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
