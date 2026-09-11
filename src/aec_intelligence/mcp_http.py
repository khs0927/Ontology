"""Stateless MCP Streamable HTTP transport for the CAIR gateway.

The transport uses JSON responses (not SSE) and delegates all semantic work to
the existing :class:`MCPGateway`. It binds to localhost by default and applies
Origin and protocol-version checks at the HTTP boundary.
"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .mcp_stdio import MCPStdioServer, SUPPORTED_PROTOCOL_VERSIONS


MAX_BODY_BYTES = 2 * 1024 * 1024


class MCPHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], repository_root: str | Path, allowed_origins: set[str] | None = None):
        self.rpc_server = MCPStdioServer(str(Path(repository_root).resolve()))
        self.allowed_origins = allowed_origins
        super().__init__(server_address, MCPRequestHandler)


class MCPRequestHandler(BaseHTTPRequestHandler):
    server: MCPHTTPServer
    protocol_version = "HTTP/1.1"

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        allowed = self.server.allowed_origins
        if allowed is None:
            host, port = self.server.server_address
            allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        return origin in allowed

    def _send_json(self, status: int, payload: dict[str, Any] | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _rpc_error(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def do_OPTIONS(self) -> None:
        if not self._origin_allowed():
            self._send_json(HTTPStatus.FORBIDDEN, self._rpc_error(None, -32000, "Invalid Origin"))
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, MCP-Protocol-Version")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._origin_allowed():
            self._send_json(HTTPStatus.FORBIDDEN, self._rpc_error(None, -32000, "Invalid Origin"))
            return
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED)
        self.close_connection = True

    def do_POST(self) -> None:
        if not self._origin_allowed():
            self._send_json(HTTPStatus.FORBIDDEN, self._rpc_error(None, -32000, "Invalid Origin"))
            return
        if self.path != "/mcp":
            self._send_json(HTTPStatus.NOT_FOUND, self._rpc_error(None, -32601, "MCP endpoint not found"))
            return
        accept = self.headers.get("Accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            self._send_json(HTTPStatus.NOT_ACCEPTABLE, self._rpc_error(None, -32600, "Accept must include application/json and text/event-stream"))
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, self._rpc_error(None, -32600, "Content-Type must be application/json"))
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(HTTPStatus.BAD_REQUEST, self._rpc_error(None, -32600, "invalid or oversized request body"))
            return
        try:
            message = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, self._rpc_error(None, -32700, f"Parse error: {exc}"))
            return
        if not isinstance(message, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, self._rpc_error(None, -32600, "Streamable HTTP accepts one JSON-RPC message per POST"))
            return
        method = message.get("method")
        if method != "initialize" and self.server.rpc_server.initialized:
            version = self.headers.get("MCP-Protocol-Version")
            if version not in SUPPORTED_PROTOCOL_VERSIONS:
                self._send_json(HTTPStatus.BAD_REQUEST, self._rpc_error(message.get("id"), -32602, "MCP-Protocol-Version is required after initialize"))
                return
        response = self.server.rpc_server.handle(message)
        if response is None:
            self._send_json(HTTPStatus.ACCEPTED)
        else:
            self._send_json(HTTPStatus.OK, response)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep MCP responses on the wire only; diagnostics belong on stderr.
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aec-mcp-http", description="AEC CAIR MCP Streamable HTTP server")
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (localhost by default)")
    parser.add_argument("--port", type=int, default=8765, help="bind port")
    parser.add_argument("--allow-origin", action="append", default=None, help="allowed Origin; may be repeated")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("refusing non-localhost bind; configure a reviewed deployment front door instead")
    server = MCPHTTPServer((args.host, args.port), args.root, set(args.allow_origin) if args.allow_origin else None)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
