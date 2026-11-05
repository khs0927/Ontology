"""Deterministic, offline stand-in for a local Ollama server (E2E smoke tests only).

It answers the endpoints the stack calls -- ``/api/embeddings`` and ``/api/embed`` (Ollama),
``/v1/embeddings`` (OpenAI-compatible), ``/api/chat`` and ``/v1/chat/completions`` -- so the whole
write and answer path can run where no model can be downloaded (CI, sandboxes).

The vectors are SHA-256 expansions, NOT semantic embeddings, and the "LLM" only echoes a citation
marker. Never point a real deployment at it. Bind is loopback only.

    python scripts/e2e/ollama_stub.py --port 11434
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = 1024
REFUSAL = "제공된 도면 데이터에서 근거를 찾을 수 없습니다."


def vector(text: str, dim: int = DIM) -> list[float]:
    values: list[float] = []
    counter = 0
    while len(values) < dim:
        digest = hashlib.sha256(f"{counter}:{text}".encode("utf-8")).digest()
        counter += 1
        values.extend((byte - 127.5) / 127.5 for byte in digest)
    values = values[:dim]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def _inputs(body: dict) -> list[str]:
    value = body.get("input", body.get("prompt", ""))
    return [value] if isinstance(value, str) else [str(v) for v in value]


def _answer(body: dict) -> str:
    messages = body.get("messages") or []
    user = str(messages[-1].get("content", "")) if messages else ""
    return "자료에 근거한 답입니다 [C1]." if "[C1]" in user else REFUSAL


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # /api/tags, /api/version, health probes
        self._send({"models": [{"name": "bge-m3"}, {"name": "qwen3:8b"}], "version": "e2e-stub"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send({"error": "invalid json"}, 400)
        if self.path == "/api/embeddings":
            return self._send({"embedding": vector(str(body.get("prompt", "")))})
        if self.path == "/api/embed":
            return self._send({"embeddings": [vector(t) for t in _inputs(body)], "model": body.get("model")})
        if self.path == "/v1/embeddings":
            data = [{"index": i, "embedding": vector(t)} for i, t in enumerate(_inputs(body))]
            return self._send({"data": data, "model": body.get("model")})
        if self.path == "/api/chat":
            return self._send({"message": {"role": "assistant", "content": _answer(body)}, "done": True,
                               "model": body.get("model")})
        if self.path == "/v1/chat/completions":
            return self._send({"choices": [{"message": {"role": "assistant", "content": _answer(body)}}],
                               "model": body.get("model")})
        return self._send({"error": f"unknown path {self.path}"}, 404)


def serve(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=11434)
    args = parser.parse_args(argv)
    server = serve(args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
