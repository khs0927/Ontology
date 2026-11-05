"""End-to-end smoke run of the whole stack against a disposable PostgreSQL database.

Exercises, as real processes (no mocks inside the product code):

1. prototype CLI   init -> ingest-dxf/ifc/geojson -> parse-reference/raster -> audit/validate/memory
                   -> rebuild-global/refresh-derived/dashboard (file-based CAIR repository in a temp dir)
2. MCP stdio       initialize, tools/list, tools/call aec.validate_repository
3. operational     init-db (twice: idempotent) -> ingest -> worker --once -> search -> kg-build -> kg-stats
4. knowledge pack  library/archioffice full load (objects + text vectors + relink) and a cited Graph RAG answer
5. REST API        uvicorn: /healthz, bearer auth, ingestion import-root guard, search, ask, kg stats
6. MCP HTTP        initialize, protocol-version header, graph_rag_query against the database

Models: an offline deterministic stub (scripts/e2e/ollama_stub.py) stands in for Ollama, so the run
needs no network and no model download. Its vectors are not semantic; this proves wiring, not quality.

    python scripts/e2e/e2e_smoke.py --dsn postgresql://aec@127.0.0.1:55432/aec_e2e

Use a FRESHLY CREATED throwaway database whose name contains 'e2e' or 'test' (enforced unless
--allow-any-db). The run adds rows and refuses a database that already holds ingestion jobs, because a
second run would be deduplicated against the first and prove nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ollama_stub  # noqa: E402

PY = sys.executable
TOKEN = "e2e-smoke-token"


class Run:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def check(self, name: str, ok: bool, detail=None) -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail not in (None, "") else ""),
              flush=True)
        return ok

    @property
    def failed(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"]]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(args, *, env=None, cwd=None, timeout=600, stdin=None) -> subprocess.CompletedProcess:
    return subprocess.run(args, env=env, cwd=cwd, timeout=timeout, input=stdin, capture_output=True,
                          text=True, encoding="utf-8")


def last_json(text: str):
    """The CLI prints one JSON document (possibly pretty-printed); parse the outermost object."""
    text = text.strip()
    start = text.find("{")
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return None


def http(method: str, url: str, body=None, headers=None, timeout=120):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json",
                                                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def wait_http(url: str, seconds: float = 30) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return True
        except urllib.error.HTTPError:
            return True  # the server answered
        except OSError:
            time.sleep(0.3)
    return False


# --------------------------------------------------------------------------- stages
def stage_prototype(r: Run, work: Path, env: dict) -> None:
    shutil.copytree(REPO / "fixtures", work / "fixtures")
    cli = [PY, "-m", "aec_intelligence.cli", "--root", str(work)]
    steps = [
        ("init", ["init", str(work)], {"INITIALIZED"}),
        ("ingest-dxf", ["ingest-dxf", "fixtures/simple_house.dxf", "--project-id", "AEC-2026-000001",
                        "--name", "Simple House"], {"SUCCESS", "SUCCESS_WITH_WARNINGS"}),
        ("ingest-file ifc", ["ingest-file", "fixtures/simple_house.ifc", "--project-id", "AEC-2026-000002",
                             "--name", "IFC House"], {"SUCCESS", "SUCCESS_WITH_WARNINGS"}),
        ("ingest-file geojson", ["ingest-file", "fixtures/site.geojson", "--project-id", "AEC-2026-000003",
                                 "--name", "Site"], {"SUCCESS", "SUCCESS_WITH_WARNINGS"}),
        ("parse-reference svg", ["parse-reference", "fixtures/simple_house.svg"], {"SUCCESS", "SUCCESS_WITH_WARNINGS"}),
        ("parse-raster dem", ["parse-raster", "fixtures/simple_terrain.dem"], {"SUCCESS", "SUCCESS_WITH_WARNINGS"}),
        ("audit", ["audit"], {"PASS"}),
        ("validate", ["validate"], {"SUCCESS"}),
        ("memory", ["memory"], {"SUCCESS"}),
        ("rebuild-global", ["rebuild-global"], {"SUCCESS"}),
        ("refresh-derived", ["refresh-derived"], {"SUCCESS"}),
        ("dashboard", ["dashboard"], {"SUCCESS"}),
    ]
    for name, args, accepted in steps:
        res = run(cli + args, env=env, cwd=work)
        doc = last_json(res.stdout) or {}
        r.check(f"prototype {name}", res.returncode == 0 and doc.get("status") in accepted,
                doc.get("status") or (res.stderr.strip().splitlines() or ["no output"])[-1])


def stage_mcp_stdio(r: Run, work: Path, env: dict) -> None:
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "aec.validate_repository", "arguments": {}}},
    ]
    res = run([PY, "-m", "aec_intelligence.mcp_stdio", "--root", str(work)], env=env,
              stdin="".join(json.dumps(m) + "\n" for m in msgs), timeout=180)
    replies = {}
    for line in res.stdout.splitlines():
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        replies[doc.get("id")] = doc
    tools = (replies.get(2) or {}).get("result", {}).get("tools", [])
    r.check("mcp stdio initialize", "result" in (replies.get(1) or {}))
    r.check("mcp stdio tools/list", len(tools) > 0, f"{len(tools)} tools")
    call = (replies.get(3) or {}).get("result", {}).get("structuredContent", {})
    r.check("mcp stdio validate_repository", call.get("status") == "SUCCESS", call.get("status"))


def database_name(dsn: str) -> str:
    """dbname of a URL or key/value libpq DSN (libpq defaults it to the user name)."""
    from psycopg.conninfo import conninfo_to_dict

    params = conninfo_to_dict(dsn)
    return str(params.get("dbname") or params.get("user") or os.environ.get("PGDATABASE") or "")


def job_states(dsn: str, job_ids: list[str]) -> dict[str, str]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute("SELECT id::text, state FROM aec.jobs WHERE id = ANY(%s::uuid[])", (job_ids,)).fetchall()
    return {row[0]: row[1] for row in rows}


def existing_jobs(dsn: str) -> int:
    import psycopg

    with psycopg.connect(dsn) as conn:
        if conn.execute("SELECT to_regclass('aec.jobs')").fetchone()[0] is None:
            return 0
        return conn.execute("SELECT count(*) FROM aec.jobs").fetchone()[0]


JOB_ID = re.compile(r"Job ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def stage_operational(r: Run, env: dict, dsn: str) -> None:
    op = [PY, "-m", "aec_intelligence.operational.cli"]
    for attempt in (1, 2):
        res = run(op + ["init-db"], env=env)
        r.check(f"init-db run {attempt}", res.returncode == 0, (res.stdout + res.stderr).strip().splitlines()[-1:])
    job_ids: list[str] = []
    for path, project in (("fixtures/simple_house.dxf", "E2E-HOUSE"), ("fixtures/simple_house.ifc", "E2E-IFC")):
        res = run(op + ["ingest", str(REPO / path), "--project-id", project, "--discipline", "architecture"], env=env)
        job_ids += JOB_ID.findall(res.stdout)
        r.check(f"enqueue {Path(path).name}", res.returncode == 0 and "Enqueued" in res.stdout, res.stdout.strip()[-120:])
    runs = 0
    for _ in range(10):  # a failing job is retried (max attempts), so bound the loop and check states after
        res = run(op + ["worker", "--once"], env=env)
        if res.returncode != 0 or "True" not in res.stdout:
            break
        runs += 1
    states = job_states(dsn, job_ids) if job_ids else {}
    r.check("worker ingested every job", len(job_ids) == 2 and set(states.values()) == {"SUCCEEDED"},
            {"worker_runs": runs, "states": sorted(states.values())})
    res = run(op + ["search", "벽체", "--project-id", "E2E-HOUSE", "--top-k", "3"], env=env)
    doc = last_json(res.stdout) or {}
    r.check("search 벽체", res.returncode == 0 and doc.get("total_hits", 0) >= 1, f"hits={doc.get('total_hits')}")
    res = run(op + ["search", "External Wall", "--project-id", "E2E-IFC", "--top-k", "3"], env=env)
    doc = last_json(res.stdout) or {}
    r.check("search IFC project", res.returncode == 0 and doc.get("total_hits", 0) >= 1, f"hits={doc.get('total_hits')}")
    res = run(op + ["kg-build"], env=env)
    r.check("kg-build", res.returncode == 0, (res.stderr.strip().splitlines() or [""])[-1][:160])
    res = run(op + ["kg-stats"], env=env)
    doc = last_json(res.stdout) or {}
    nodes = sum((doc.get("nodes") or {}).values())
    r.check("kg-stats", res.returncode == 0 and nodes > 0, f"nodes={nodes}")


def stage_knowledge_pack(r: Run, env: dict, dsn: str) -> None:
    loader = REPO / "library" / "archioffice" / "tools" / "load_archioffice.py"
    res = run([PY, str(REPO / "library" / "archioffice" / "tools" / "verify_pack.py")], env=env)
    findings = (last_json(res.stdout) or {}).get("findings")
    r.check("archioffice verify_pack", res.returncode == 0 and findings == ["NONE"], findings)
    for attempt in (1, 2):  # second run proves the prune/relink path is idempotent
        res = run([PY, str(loader), "--apply", "--dsn", dsn], env=env, timeout=1800)
        applied = [ln for ln in res.stdout.splitlines() if ln.startswith("APPLIED")]
        r.check(f"archioffice full load run {attempt}", res.returncode == 0 and bool(applied),
                applied[-1] if applied else (res.stderr.strip().splitlines() or ["no output"])[-1][:200])
    op = [PY, "-m", "aec_intelligence.operational.cli"]
    res = run(op + ["ask", "CAD 명령 단축키 3DO", "--project", "ARCHIOFFICE", "--json"], env=env, timeout=300)
    doc = last_json(res.stdout) or {}
    r.check("graph rag cited answer (pack)", not doc.get("refused", True) and len(doc.get("citations") or []) >= 1,
            {"route": doc.get("route"), "mode": doc.get("answer_mode"), "citations": len(doc.get("citations") or [])})
    res = run(op + ["ask", "이 건물 시공사 대표 전화번호는?", "--project", "ARCHIOFFICE", "--json"], env=env, timeout=300)
    doc = last_json(res.stdout) or {}
    r.check("graph rag refuses ungrounded question", doc.get("refused") is True, doc.get("warnings"))


def log_tail(path: Path, lines: int = 8) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def stage_api(r: Run, env: dict, logs: Path) -> None:
    port = free_port()
    log = logs / "api.log"
    api_env = {**env, "AEC_API_TOKEN": TOKEN, "AEC_IMPORT_ROOTS": str(REPO / "fixtures")}
    proc = subprocess.Popen([PY, "-m", "uvicorn", "aec_intelligence.operational.api:create_app", "--factory",
                             "--host", "127.0.0.1", "--port", str(port)], env=api_env,
                            stdout=subprocess.DEVNULL, stderr=log.open("w", encoding="utf-8"))
    base = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {TOKEN}"}
    try:
        if not r.check("api starts", wait_http(base + "/healthz", 60), None if proc.poll() is None else log_tail(log)):
            return
        status, body = http("GET", base + "/healthz")
        r.check("api /healthz", status == 200 and json.loads(body).get("status") == "ok", status)
        status, _ = http("GET", base + "/v1/stats")
        r.check("api rejects missing token", status == 401, status)
        status, body = http("GET", base + "/v1/stats", headers=auth)
        r.check("api /v1/stats", status == 200 and json.loads(body).get("objects", 0) > 0, status)
        status, _ = http("POST", base + "/v1/ingestions", {"path": "/etc/hostname"}, headers=auth)
        r.check("api import-root guard", status in (400, 403), status)
        status, body = http("POST", base + "/v1/ingestions",
                            {"path": str(REPO / "fixtures" / "simple_house_geometry.ifc"), "project_id": "E2E-API"}, headers=auth)
        r.check("api enqueue ingestion", status == 202, status)
        status, body = http("POST", base + "/v1/search", {"query": "벽체", "top_k": 3}, headers=auth)
        r.check("api search", status == 200 and json.loads(body).get("total_hits", 0) >= 1, status)
        status, body = http("POST", base + "/v1/ask", {"question": "CAD 명령 단축키 3DO", "project": "ARCHIOFFICE"},
                            headers=auth, timeout=300)
        doc = json.loads(body) if status == 200 else {}
        r.check("api ask", status == 200 and not doc.get("refused", True), {"status": status, "mode": doc.get("answer_mode")})
        status, body = http("GET", base + "/v1/kg/stats", headers=auth)
        r.check("api kg stats", status == 200 and json.loads(body).get("projects", 0) >= 1, status)
        status, body = http("GET", base + "/dashboard", headers=auth)
        r.check("api dashboard html", status == 200 and "<html" in body.lower(), status)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def stage_mcp_http(r: Run, work: Path, env: dict, logs: Path) -> None:
    port = free_port()
    log = logs / "mcp_http.log"
    proc = subprocess.Popen([PY, "-m", "aec_intelligence.mcp_http", "--root", str(work), "--port", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=log.open("w", encoding="utf-8"))
    url = f"http://127.0.0.1:{port}/mcp"
    accept = {"Accept": "application/json, text/event-stream"}
    try:
        if not r.check("mcp http starts", wait_http(url, 30), None if proc.poll() is None else log_tail(log)):
            return
        status, body = http("POST", url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}}},
            headers=accept)
        r.check("mcp http initialize", status == 200 and "result" in json.loads(body), status)
        status, body = http("POST", url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=accept)
        r.check("mcp http requires protocol header", "error" in json.loads(body or "{}"), status)
        hdr = {**accept, "MCP-Protocol-Version": "2025-06-18"}
        status, body = http("POST", url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "aec.graph_rag_query", "arguments": {"question": "CAD 명령 단축키 3DO", "project": "ARCHIOFFICE"}}},
            headers=hdr, timeout=300)
        sc = json.loads(body).get("result", {}).get("structuredContent", {}) if status == 200 else {}
        r.check("mcp http graph_rag_query", sc.get("status") == "SUCCESS" and not sc.get("refused", True),
                sc.get("status"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AEC stack end-to-end smoke run")
    parser.add_argument("--dsn", default=os.environ.get("AEC_E2E_DATABASE_URL"),
                        help="disposable database URL (or AEC_E2E_DATABASE_URL)")
    parser.add_argument("--allow-any-db", action="store_true", help="skip the throwaway-database name check")
    parser.add_argument("--report", help="write the JSON report here")
    parser.add_argument("--keep", action="store_true", help="keep the temporary work directory")
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("--dsn (or AEC_E2E_DATABASE_URL) is required")
    try:
        dbname = database_name(args.dsn)
    except Exception as exc:  # noqa: BLE001 - psycopg missing or an unparseable DSN
        parser.error(f"cannot read the database name from --dsn: {exc}")
    if not args.allow_any_db and not any(tag in dbname.lower() for tag in ("e2e", "test")):
        parser.error(f"refusing database {dbname!r}: name must contain 'e2e' or 'test' (or pass --allow-any-db)")
    try:
        leftover = existing_jobs(args.dsn)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot connect to {dbname!r}: {exc}", file=sys.stderr)
        return 2
    if leftover:
        print(f"refusing database {dbname!r}: it already holds {leftover} ingestion jobs; "
              "create a fresh database for each run", file=sys.stderr)
        return 2

    r = Run()
    stub_port = free_port()
    stub = ollama_stub.serve(stub_port)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    stub_url = f"http://127.0.0.1:{stub_port}"

    work = Path(tempfile.mkdtemp(prefix="aec-e2e-"))
    data = work / "_data"
    data.mkdir()
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join([str(REPO / "src"), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep),
           "PYTHONIOENCODING": "utf-8",
           "AEC_DATABASE_URL": args.dsn, "AEC_DATA_ROOT": str(data),
           "AEC_IMPORT_ROOTS": str(REPO / "fixtures"),
           "AEC_EMBEDDING_URL": "",                # offline hash model for drawing objects
           "AEC_LLM_URL": stub_url, "AEC_LLM_TIMEOUT_SECONDS": "30",
           "OLLAMA_URL": stub_url}                 # knowledge-pack loader embeddings
    started = time.monotonic()
    try:
        for stage in (lambda: stage_prototype(r, work / "repo", env),
                      lambda: stage_mcp_stdio(r, work / "repo", env),
                      lambda: stage_operational(r, env, args.dsn),
                      lambda: stage_knowledge_pack(r, env, args.dsn),
                      lambda: stage_api(r, env, work),
                      lambda: stage_mcp_http(r, work / "repo", env, work)):
            try:
                stage()
            except Exception as exc:  # noqa: BLE001 - a crashed stage is a failed check, keep going
                r.check(f"stage crashed: {type(exc).__name__}", False, str(exc)[:300])
    finally:
        stub.shutdown()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    report = {"passed": len(r.checks) - len(r.failed), "failed": len(r.failed),
              "seconds": round(time.monotonic() - started, 1), "checks": r.checks}
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nE2E: {report['passed']} passed, {report['failed']} failed in {report['seconds']}s")
    return 1 if r.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
