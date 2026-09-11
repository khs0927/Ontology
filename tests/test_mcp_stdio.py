import io
import json
from pathlib import Path
import subprocess
import sys

from aec_intelligence.mcp_stdio import MCPStdioServer


def test_mcp_stdio_lifecycle_and_tool_call(tmp_path: Path):
    server = MCPStdioServer(str(tmp_path))
    initialized = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
    assert initialized["result"]["protocolVersion"] == "2025-11-25"
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert any(tool["name"] == "aec.audit" for tool in tools["result"]["tools"])
    called = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "aec.query_plan", "arguments": {"question": "인접한 출입문"}}})
    assert called["result"]["structuredContent"]["route"] == "KNOWLEDGE_GRAPH"
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_stdio_processes_newline_delimited_messages_and_batches(tmp_path: Path):
    server = MCPStdioServer(str(tmp_path))
    request = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    batch = [{"jsonrpc": "2.0", "id": 2, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}]
    output = io.StringIO()
    server.run(io.StringIO(json.dumps(request) + "\n" + json.dumps(batch) + "\n"), output)
    lines = output.getvalue().splitlines()
    assert json.loads(lines[0])["result"] == {}
    assert json.loads(lines[1])[0]["id"] == 2


def test_mcp_stdio_cli_emits_only_jsonrpc_on_stdout(tmp_path: Path):
    payload = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]) + "\n"
    completed = subprocess.run([sys.executable, "-m", "aec_intelligence.mcp_stdio", "--root", str(tmp_path)], input=payload, text=True, capture_output=True, check=True)
    assert completed.stderr == ""
    assert [json.loads(line)["jsonrpc"] for line in completed.stdout.splitlines()] == ["2.0", "2.0"]
