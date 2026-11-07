"""Offline checks of the E2E smoke harness itself (the full run needs PostgreSQL; see CI)."""
import importlib.util
import json
import math
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

E2E = Path(__file__).resolve().parents[1] / "scripts" / "e2e"


def _load(name):
    sys.path.insert(0, str(E2E))
    try:
        spec = importlib.util.spec_from_file_location(name, E2E / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(E2E))


def test_stub_vectors_are_deterministic_unit_length():
    stub = _load("ollama_stub")
    a, b = stub.vector("벽체"), stub.vector("벽체")
    assert a == b and len(a) == stub.DIM == 1024
    assert math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-9)
    assert stub.vector("문") != a


def test_stub_serves_ollama_and_openai_shapes():
    stub = _load("ollama_stub")
    server = stub.serve(0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    try:
        assert len(post("/api/embeddings", {"prompt": "x"})["embedding"]) == 1024
        assert len(post("/api/embed", {"input": ["a", "b"]})["embeddings"]) == 2
        assert [d["index"] for d in post("/v1/embeddings", {"input": ["a", "b"]})["data"]] == [0, 1]
        cited = post("/api/chat", {"messages": [{"role": "user", "content": "[C1] 자료"}]})
        assert "[C1]" in cited["message"]["content"]
        refused = post("/api/chat", {"messages": [{"role": "user", "content": "자료 없음"}]})
        assert refused["message"]["content"] == stub.REFUSAL
    finally:
        server.shutdown()
        server.server_close()


def test_smoke_refuses_a_non_throwaway_database(capsys):
    smoke = _load("e2e_smoke")
    with pytest.raises(SystemExit) as exc:
        smoke.main(["--dsn", "postgresql://aec@127.0.0.1:55432/aec"])
    assert exc.value.code == 2
    assert "refusing database 'aec'" in capsys.readouterr().err


def test_last_json_reads_pretty_printed_cli_output():
    smoke = _load("e2e_smoke")
    assert smoke.last_json('log line\n{\n  "status": "SUCCESS"\n}\n') == {"status": "SUCCESS"}
    assert smoke.last_json("no json") is None


@pytest.mark.parametrize("dsn", ["host=e2e-db dbname=aec", "dbname=prod user=test", "postgresql://test@h/prod"])
def test_smoke_guard_reads_the_real_dbname(dsn, capsys):
    pytest.importorskip("psycopg")
    smoke = _load("e2e_smoke")
    with pytest.raises(SystemExit) as exc:
        smoke.main(["--dsn", dsn])
    assert exc.value.code == 2
    assert "refusing database" in capsys.readouterr().err
