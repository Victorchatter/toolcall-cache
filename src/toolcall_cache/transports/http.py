"""MCP HTTP transport: tiny reverse proxy with tools/call caching."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .. import cache, proxy

# Headers that should not be blindly forwarded between client and upstream.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "content-length",
    "host",
}


class HttpProxyState:
    """Shared mutable state for the HTTP proxy handler."""

    def __init__(
        self,
        upstream_base_url: str,
        db_path: str,
        server_id: str,
        allowlist: list[str],
        denylist: list[str],
        ttl: float,
    ) -> None:
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.server_id = server_id
        self.allowlist = allowlist
        self.denylist = denylist
        self.ttl = ttl
        self.conn = cache.init_db(db_path)
        self.annotations: dict[str, dict] = {}
        self.pending_calls: dict[Any, tuple[str, dict[str, Any]]] = {}
        self.lock = threading.Lock()


class HttpProxyHandler(BaseHTTPRequestHandler):
    """Request handler that forwards to an upstream MCP HTTP server."""

    state: HttpProxyState  # type: ignore[misc]

    def log_message(self, format: str, *args: Any) -> None:
        """Silence default request logging."""
        pass

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            size = int(length)
        except ValueError:
            return b""
        return self.rfile.read(size)

    def _send_json(self, status: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _build_upstream_url(self) -> str:
        path = self.path
        return f"{self.state.upstream_base_url}{path}"

    def _forward_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP:
                continue
            if lower == "host":
                continue
            headers[key] = value
        return headers

    def _handle_post(self) -> None:
        body = self._read_body()
        parsed: dict[str, Any] | None = None
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            pass

        forwarded_call: tuple[str, dict[str, Any]] | None = None
        if isinstance(parsed, dict):
            is_call, tool_name, arguments = proxy.is_tools_call_request(parsed)
            if is_call:
                with self.state.lock:
                    cached = proxy.try_cache_hit(
                        self.state.conn,
                        self.state.server_id,
                        tool_name,
                        arguments,
                        parsed,
                        self.state.annotations,
                        self.state.allowlist,
                        self.state.denylist,
                    )
                if cached is not None:
                    self._send_json(200, cached)
                    return

            msg_id = parsed.get("id")
            if is_call and msg_id is not None:
                forwarded_call = (tool_name, arguments)
                with self.state.lock:
                    self.state.pending_calls[msg_id] = (tool_name, arguments)

        try:
            upstream_url = self._build_upstream_url()
            req = urllib.request.Request(
                upstream_url,
                data=body,
                headers=self._forward_headers(),
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                resp_body = resp.read()
                resp_headers = dict(resp.headers)
        except urllib.error.HTTPError as exc:
            resp_body = exc.read()
            self._send_raw(exc.code, exc.headers, resp_body)
            return
        except urllib.error.URLError as exc:
            self._send_json(502, {"error": f"upstream failed: {exc}"})
            return

        # Try to cache successful forwarded tools/call responses.
        if forwarded_call is not None:
            self._maybe_cache_response(parsed, resp_body, forwarded_call)

        self._maybe_extract_annotations(resp_body)
        self._send_raw(200, resp_headers, resp_body)

    def _maybe_cache_response(
        self,
        request: dict[str, Any] | None,
        response_body: bytes,
        forwarded_call: tuple[str, dict[str, Any]],
    ) -> None:
        if request is None:
            return
        msg_id = request.get("id")
        if msg_id is None:
            return
        try:
            resp = json.loads(response_body.decode("utf-8"))
        except Exception:
            return
        tool_name, arguments = forwarded_call
        with self.state.lock:
            self.state.pending_calls.pop(msg_id, None)
            proxy.store_response(
                self.state.conn,
                self.state.server_id,
                tool_name,
                arguments,
                resp,
                self.state.annotations,
                self.state.allowlist,
                self.state.denylist,
                self.state.ttl,
            )

    def _maybe_extract_annotations(self, response_body: bytes) -> None:
        try:
            resp = json.loads(response_body.decode("utf-8"))
        except Exception:
            return
        if proxy.is_tools_list_response(resp):
            with self.state.lock:
                self.state.annotations.update(proxy.extract_tool_annotations(resp))

    def _send_raw(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.send_response(status)
        for key, value in headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        if "content-type" not in {k.lower() for k in headers}:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _handle_get(self) -> None:
        try:
            upstream_url = self._build_upstream_url()
            req = urllib.request.Request(upstream_url, headers=self._forward_headers(), method="GET")
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
                headers = dict(resp.headers)
            self._send_raw(resp.status, headers, body)
        except urllib.error.HTTPError as exc:
            self._send_raw(exc.code, dict(exc.headers), exc.read())
        except urllib.error.URLError as exc:
            self._send_json(502, {"error": f"upstream failed: {exc}"})

    def do_GET(self) -> None:
        self._handle_get()

    def do_POST(self) -> None:
        self._handle_post()

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def make_http_handler(state: HttpProxyState) -> type[BaseHTTPRequestHandler]:
    """Create a handler class bound to the given proxy state."""
    return type("BoundHttpProxyHandler", (HttpProxyHandler,), {"state": state})


def run_http_proxy(
    upstream_base_url: str,
    host: str,
    port: int,
    db_path: str,
    server_id: str,
    allowlist: list[str],
    denylist: list[str],
    ttl: float,
) -> None:
    """Run the HTTP reverse proxy until interrupted."""
    state = HttpProxyState(upstream_base_url, db_path, server_id, allowlist, denylist, ttl)
    handler = make_http_handler(state)
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state.conn.close()
