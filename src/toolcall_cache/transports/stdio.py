"""MCP stdio transport: proxy between agent stdin/stdout and upstream subprocess."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import threading
from typing import Any

from .. import cache, proxy


class StdioTransport:
    """Forward stdio MCP traffic, caching deterministic ``tools/call`` results."""

    def __init__(
        self,
        upstream_command: list[str],
        db_path: str,
        server_id: str,
        allowlist: list[str],
        denylist: list[str],
        ttl: float,
    ) -> None:
        self.upstream_command = upstream_command
        self.server_id = server_id
        self.allowlist = allowlist
        self.denylist = denylist
        self.ttl = ttl
        self.conn = cache.init_db(db_path)
        self.annotations: dict[str, dict] = {}
        # Pending forwarded tools/call requests: id -> (tool_name, arguments)
        self.pending_calls: dict[Any, tuple[str, dict[str, Any]]] = {}

        self.agent_in_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.upstream_out_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.stdin_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.stdout_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        self.upstream_proc: asyncio.subprocess.Process | None = None
        self._shutdown = False
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self) -> None:
        """Start the upstream subprocess and run the proxy loop."""
        self._loop = asyncio.get_running_loop()
        self.upstream_proc = await asyncio.create_subprocess_exec(
            *self.upstream_command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Stdin is a real OS file; read it in a thread so we don't depend on the
        # event loop's pipe support on every platform. Upstream stdout is an
        # asyncio StreamReader, so we read it with asyncio.
        agent_reader = threading.Thread(target=self._read_agent_stdin, daemon=True)
        agent_reader.start()

        try:
            await asyncio.gather(
                self._process_agent_messages(),
                self._process_upstream_messages(),
                self._write_upstream_stdin(),
                self._write_agent_stdout(),
                self._read_upstream_stdout(),
            )
        finally:
            await self._stop_upstream()

    def _read_agent_stdin(self) -> None:
        """Blocking reader thread for agent stdin."""
        loop = self._loop
        assert loop is not None
        try:
            for line in sys.stdin.buffer:
                if self._shutdown:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                asyncio.run_coroutine_threadsafe(self.agent_in_queue.put(msg), loop)
        finally:
            asyncio.run_coroutine_threadsafe(self.agent_in_queue.put(None), loop)

    async def _read_upstream_stdout(self) -> None:
        """Async reader for upstream stdout."""
        assert self.upstream_proc is not None
        assert self.upstream_proc.stdout is not None
        while True:
            line = await self.upstream_proc.stdout.readline()
            if not line:
                await self.upstream_out_queue.put(None)
                break
            try:
                msg = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            await self.upstream_out_queue.put(msg)

    async def _process_agent_messages(self) -> None:
        while True:
            msg = await self.agent_in_queue.get()
            if msg is None:
                await self.stdin_queue.put(None)
                await self.stdout_queue.put(None)
                await self._stop_upstream()
                break

            is_call, tool_name, arguments = proxy.is_tools_call_request(msg)
            if is_call:
                cached = proxy.try_cache_hit(
                    self.conn,
                    self.server_id,
                    tool_name,
                    arguments,
                    msg,
                    self.annotations,
                    self.allowlist,
                    self.denylist,
                )
                if cached is not None:
                    await self.stdout_queue.put(cached)
                    continue

            # Forward anything else (cache miss or non-tool-call) upstream.
            msg_id = msg.get("id")
            if is_call and msg_id is not None:
                self.pending_calls[msg_id] = (tool_name, arguments)
            await self.stdin_queue.put(msg)

    async def _process_upstream_messages(self) -> None:
        while True:
            msg = await self.upstream_out_queue.get()
            if msg is None:
                await self.stdout_queue.put(None)
                break

            msg_id = msg.get("id")
            pending = self.pending_calls.pop(msg_id, None)
            if pending is not None:
                tool_name, arguments = pending
                proxy.store_response(
                    self.conn,
                    self.server_id,
                    tool_name,
                    arguments,
                    msg,
                    self.annotations,
                    self.allowlist,
                    self.denylist,
                    self.ttl,
                )

            if proxy.is_tools_list_response(msg):
                self.annotations.update(proxy.extract_tool_annotations(msg))

            await self.stdout_queue.put(msg)

    async def _write_upstream_stdin(self) -> None:
        assert self.upstream_proc is not None
        assert self.upstream_proc.stdin is not None
        while True:
            msg = await self.stdin_queue.get()
            if msg is None:
                break
            line = (json.dumps(msg) + "\n").encode("utf-8")
            self.upstream_proc.stdin.write(line)
            await self.upstream_proc.stdin.drain()

    async def _write_agent_stdout(self) -> None:
        while True:
            msg = await self.stdout_queue.get()
            if msg is None:
                break
            line = (json.dumps(msg) + "\n").encode("utf-8")
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    async def _stop_upstream(self) -> None:
        self._shutdown = True
        proc = self.upstream_proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception:
            pass


def run_stdio_proxy(
    upstream_command: str,
    db_path: str,
    server_id: str,
    allowlist: list[str],
    denylist: list[str],
    ttl: float,
) -> None:
    """Parse the upstream command and run the stdio proxy until stdin closes."""
    # Windows paths contain backslashes; shlex.split(posix=True) strips them.
    # ponytail: posix=False keeps backslashes literal, which is what Windows
    # command lines need.
    if sys.platform == "win32":
        cmd = shlex.split(upstream_command, posix=False)
    else:
        cmd = shlex.split(upstream_command)
    transport = StdioTransport(cmd, db_path, server_id, allowlist, denylist, ttl)
    asyncio.run(transport.run())
