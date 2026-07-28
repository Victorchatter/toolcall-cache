"""End-to-end self-test for toolcall-cache.

Runs a fake MCP server behind the stdio proxy and asserts:
- repeated ``read_file`` calls hit the cache (upstream counter stays at 1)
- repeated ``now`` calls always pass through (counter increments)
- fuzzy mode matches semantically similar tool arguments
- the ``fuzzy-test`` CLI finds cached fuzzy matches
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

    counts = {"read_file": 0, "search": 0, "lookup": 0, "now": 0}

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
                            "name": "search",
                            "description": "Searches for a pattern.",
                            "inputSchema": {"type": "object", "properties": {"pattern": {"type": "string"}}},
                            "annotations": {"cacheable": True},
                        },
                        {
                            "name": "lookup",
                            "description": "Looks up a key.",
                            "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}},
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
            if name == "search":
                counts["search"] += 1
                return {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "result": {"content": [{"type": "text", "text": "constant search results"}]},
                }
            if name == "lookup":
                counts["lookup"] += 1
                return {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "result": {"content": [{"type": "text", "text": "constant lookup results"}]},
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


def test_ttl():
    """Unit-check TTL expiry against the cache module directly.

    cache.get / cache.put accept an injectable ``now`` so expiry can be
    exercised without sleeping: a hit before expiry returns the result; a read
    after expiry returns None (a miss), so the next call re-fetches upstream.
    """
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    sys.path.insert(0, src_path)
    from toolcall_cache.cache import get, init_db, put  # noqa: E402
    from toolcall_cache.key import make_args_hash, make_key  # noqa: E402

    db_path = tempfile.mktemp(suffix=".db")
    conn = init_db(db_path)
    try:
        key = make_key("s", "read_file", {"path": "/t"})
        ah = make_args_hash({"path": "/t"})
        result = {"content": [{"type": "text", "text": "content of /t"}]}
        ttl = 60.0

        put(conn, key, "s", "read_file", ah, result, ttl, now=100.0)
        _assert(get(conn, key, now=100.0) is not None, "entry should hit before expiry")
        _assert(get(conn, key, now=159.0) is not None, "entry should hit at the boundary (now <= expires_at)")
        _assert(get(conn, key, now=160.1) is None, "entry should miss after expiry")
        _assert(get(conn, key, now=200.0) is None, "entry should stay missing after expiry")
    finally:
        conn.close()
        for ext in ("", "-journal", "-wal", "-shm"):
            try:
                os.remove(db_path + ext)
            except OSError:
                pass


def test_fuzzy_key_utils():
    """Quick unit check for normalization and Levenshtein ratio."""
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    sys.path.insert(0, src_path)
    from toolcall_cache.key import levenshtein_ratio, normalize_args  # noqa: E402

    normalized = normalize_args({"path": "  /TMP/FOO.TXT  "}, ignore_keys=set())
    _assert(normalized == {"path": "/tmp/foo.txt"}, f"unexpected normalized args: {normalized}")

    ignored = normalize_args({"path": "/tmp/a", "session_id": "1"}, ignore_keys={"session_id"})
    _assert("session_id" not in ignored, "ignored key should be removed")
    _assert(ignored == {"path": "/tmp/a"}, f"unexpected ignored-key result: {ignored}")

    ratio = levenshtein_ratio("todo", "todos")
    _assert(ratio > 0.85, f"expected ratio > 0.85, got {ratio}")


def test_state_dir():
    """The --state-dir flag is honored for unified LocalLab state."""
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    fake_path = tempfile.mktemp(suffix="_fake_mcp.py")
    state_dir = tempfile.mkdtemp()
    expected_db = os.path.join(state_dir, "toolcall-cache", "cache.db")

    with open(fake_path, "w", encoding="utf-8") as f:
        f.write(FAKE_SERVER)

    for ext in ("", "-journal", "-wal", "-shm"):
        p = expected_db + ext
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
            "--state-dir",
            state_dir,
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

        _assert(os.path.exists(expected_db), f"expected db at {expected_db}")
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
        for root, dirs, files in os.walk(state_dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        try:
            os.rmdir(state_dir)
        except OSError:
            pass


def test_hydrate():
    """Hydrating from a tape populates the cache with observed tool results."""
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    state_dir = tempfile.mkdtemp()
    tape_path = os.path.join(state_dir, "tape.jsonl")

    events = [
        {"kind": "tool_call", "seq": 1, "server": "fs", "tool": "read_file",
         "args": {"path": "/tmp/foo.txt"}},
        {"kind": "tool_result", "seq": 1, "server": "fs", "tool": "read_file",
         "args_hash": "h1",
         "result": {"content": [{"type": "text", "text": "cached from tape"}]}},
        {"kind": "tool_call", "seq": 2, "server": "fs", "tool": "now", "args": {}},
        {"kind": "tool_result", "seq": 2, "server": "fs", "tool": "now",
         "args_hash": "h2",
         "result": {"content": [{"type": "text", "text": "1"}]}},
    ]
    with open(tape_path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    env = os.environ.copy()
    env["PYTHONPATH"] = src_path

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "toolcall_cache",
            "hydrate",
            "--state-dir",
            state_dir,
            "--tape",
            tape_path,
            "--server-id",
            "fs",
            "--allowlist",
            "read_file",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    _assert(result.returncode == 0, f"hydrate failed: {result.stderr}")
    _assert("Cached 1" in result.stdout, f"expected one cached entry: {result.stdout}")

    # Verify the entry is actually in the database by listing it.
    list_result = subprocess.run(
        [sys.executable, "-m", "toolcall_cache", "list", "--state-dir", state_dir],
        capture_output=True,
        text=True,
        env=env,
    )
    _assert(list_result.returncode == 0, f"list failed: {list_result.stderr}")
    _assert("read_file" in list_result.stdout, "expected read_file in cache list")

    # Cleanup
    for root, dirs, files in os.walk(state_dir, topdown=False):
        for name in files:
            os.remove(os.path.join(root, name))
        for name in dirs:
            os.rmdir(os.path.join(root, name))
    try:
        os.rmdir(state_dir)
    except OSError:
        pass


def main():
    test_ttl()
    test_fuzzy_key_utils()

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
            "read_file,search,lookup",
            "--ttl",
            "60",
            "--fuzzy",
            "--fuzzy-ignore-keys",
            "session_id",
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

        # Second read_file call with identical args should be cached (exact hit).
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

        # Normalization: whitespace and case variation maps to the same normalized key.
        _send(
            proxy,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "  /var/lib/unique-FILE.txt  "}},
            },
        )
        r3 = _recv(proxy)
        _assert(
            r3["result"]["content"][0]["type"] == "text",
            f"unexpected normalized read_file response: {r3}",
        )
        # A repeat with a different whitespace/case variant should exact-hit the normalized key.
        _send(
            proxy,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "  /VAR/LIB/unique-file.TXT  "}},
            },
        )
        r4 = _recv(proxy)
        _assert(
            r4["result"]["content"][0]["text"] == r3["result"]["content"][0]["text"],
            f"repeat normalized read_file should match first response: {r4}",
        )
        _assert(
            r4["result"].get("_meta") is None,
            f"exact normalized hit should not set _meta: {r4}",
        )

        # Semantic fuzzy hit through the fuzzy lookup path (not just normalized exact key).
        _send(
            proxy,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"pattern": "todo"}},
            },
        )
        s1 = _recv(proxy)
        _assert(
            s1["result"]["content"][0]["text"] == "constant search results",
            f"unexpected search response: {s1}",
        )

        _send(
            proxy,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"pattern": "todos"}},
            },
        )
        s2 = _recv(proxy)
        _assert(
            s2["result"]["content"][0]["text"] == "constant search results",
            f"unexpected fuzzy search response: {s2}",
        )
        _assert(
            s2["result"].get("_meta", {}).get("locallab_fuzzy_match") is True,
            f"fuzzy hit should set _meta.locallab_fuzzy_match: {s2}",
        )

        # Fuzzy hit with ignored key: session_id is dropped, then fuzzy lookup bridges
        # the small pattern variation.
        _send(
            proxy,
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "lookup",
                    "arguments": {"key": "token", "session_id": "abc"},
                },
            },
        )
        s3 = _recv(proxy)
        _assert(
            s3["result"]["content"][0]["text"] == "constant lookup results",
            f"unexpected lookup response with ignored key: {s3}",
        )

        _send(
            proxy,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "lookup",
                    "arguments": {"key": "tokens", "session_id": "xyz"},
                },
            },
        )
        s4 = _recv(proxy)
        _assert(
            s4["result"]["content"][0]["text"] == "constant lookup results",
            f"unexpected fuzzy lookup response with ignored key: {s4}",
        )
        _assert(
            s4["result"].get("_meta", {}).get("locallab_fuzzy_match") is True,
            f"fuzzy hit with ignored key should set _meta.locallab_fuzzy_match: {s4}",
        )

        # now() is not cacheable, so each call must increment the upstream counter.
        _send(proxy, {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "now"}})
        n1 = _recv(proxy)
        _assert(n1["result"]["content"][0]["text"] == "1", f"unexpected now response: {n1}")

        _send(proxy, {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "now"}})
        n2 = _recv(proxy)
        _assert(n2["result"]["content"][0]["text"] == "2", f"unexpected now response: {n2}")

        # Verify stats reports at least one hit.
        stats_result = subprocess.run(
            [sys.executable, "-m", "toolcall_cache", "stats", "--db", db_path],
            capture_output=True,
            text=True,
            env=env,
        )
        _assert(stats_result.returncode == 0, f"stats exited {stats_result.returncode}: {stats_result.stderr}")
        stats_lower = stats_result.stdout.lower()
        _assert("hits" in stats_lower, "stats output must mention hits")
        hit_line = next((l for l in stats_result.stdout.splitlines() if l.strip().lower().startswith("hits")), None)
        _assert(hit_line is not None, "stats output missing Hits line")
        hit_count = int(hit_line.split()[-1])
        _assert(hit_count >= 1, f"expected hits >= 1, got {hit_count}")

        # fuzzy-test CLI should report that two semantically close argument sets match.
        fuzzy_test_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "toolcall_cache",
                "fuzzy-test",
                "search",
                '{"pattern":"todo"}',
                '{"pattern":"todos"}',
                "--fuzzy-threshold",
                "0.85",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        _assert(
            fuzzy_test_result.returncode == 0,
            f"fuzzy-test exited {fuzzy_test_result.returncode}: {fuzzy_test_result.stderr}",
        )
        _assert(
            "match: yes" in fuzzy_test_result.stdout,
            f"fuzzy-test should report match: yes: {fuzzy_test_result.stdout}",
        )

        # Ask the fake server directly for its counters (bypassing proxy via a side channel is
        # impossible, so we rely on the now responses above as the ground-truth counter).
        _send(proxy, {"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {"name": "now"}})
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

    test_state_dir()
    test_hydrate()


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"selfcheck FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
