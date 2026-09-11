"""Minimal MCP JSON-RPC stdio transport for the CAIR gateway.

The implementation follows the MCP newline-delimited stdio shape while
keeping semantic operations in :mod:`aec_intelligence.mcp_gateway`.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .mcp_gateway import MCPGateway


SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2024-11-05")


def _success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


class MCPStdioServer:
    def __init__(self, repository_root: str):
        self.gateway = MCPGateway(repository_root)
        self.initialized = False
        self.running = True

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return None if "id" not in message else _error(request_id, -32600, "Invalid Request")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Invalid params") if "id" in message else None

        if method == "initialize":
            requested = params.get("protocolVersion")
            protocol_version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
            self.initialized = True
            return _success(request_id, {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "aec-cair-gateway", "version": "0.1.0"},
                "instructions": "CAIR is canonical; runtime and application adapters are rebuildable consumers.",
            }) if "id" in message else None
        if method in {"notifications/initialized", "notifications/cancelled", "notifications/progress"}:
            return None
        if method == "ping":
            return _success(request_id, {}) if "id" in message else None
        if method == "shutdown":
            return _success(request_id, None) if "id" in message else None
        if method == "exit":
            self.running = False
            return None
        if method == "tools/list":
            return _success(request_id, {"tools": self.gateway.list_tools()}) if "id" in message else None
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _error(request_id, -32602, "tools/call requires a tool name and object arguments") if "id" in message else None
            if name not in {tool["name"] for tool in self.gateway.list_tools()}:
                return _error(request_id, -32602, f"Unknown tool: {name}") if "id" in message else None
            output = self.gateway.call_tool(name, arguments)
            is_error = output.get("status") == "FAILED"
            result: dict[str, Any] = {
                "content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False, separators=(",", ":"))}],
                "structuredContent": output,
            }
            if is_error:
                result["isError"] = True
            return _success(request_id, result) if "id" in message else None
        return _error(request_id, -32601, f"Method not found: {method}") if "id" in message else None

    def run(self, input_stream: Any = None, output_stream: Any = None) -> int:
        input_stream = input_stream or sys.stdin
        output_stream = output_stream or sys.stdout
        for raw_line in input_stream:
            if not self.running:
                break
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                messages = payload if isinstance(payload, list) else [payload]
                responses = [self.handle(message) if isinstance(message, dict) else _error(None, -32600, "Invalid Request") for message in messages]
                responses = [response for response in responses if response is not None]
                if isinstance(payload, list):
                    if responses:
                        output_stream.write(json.dumps(responses, ensure_ascii=False, separators=(",", ":")) + "\n")
                elif responses:
                    output_stream.write(json.dumps(responses[0], ensure_ascii=False, separators=(",", ":")) + "\n")
                output_stream.flush()
            except json.JSONDecodeError as exc:
                output_stream.write(json.dumps(_error(None, -32700, "Parse error", str(exc)), ensure_ascii=False, separators=(",", ":")) + "\n")
                output_stream.flush()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aec-mcp", description="AEC CAIR MCP stdio server")
    parser.add_argument("--root", default=".", help="repository root")
    args = parser.parse_args(argv)
    return MCPStdioServer(args.root).run()


if __name__ == "__main__":
    raise SystemExit(main())
