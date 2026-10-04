"""Phase 4 entity resolution and query routing rules (pure functions, no database)."""

import json

import pytest

from aec_intelligence.operational.graphrag.ask import REFUSAL, choose_route, link_static
from aec_intelligence.operational.graphrag.llm import LLMError, LLMConfig, LocalLLM, is_local_endpoint
from aec_intelligence.operational.graphrag.resolve import (
    canonical_project,
    canonical_room,
    canonical_section,
    date_token,
    drawing_fields,
    series_key,
)

@pytest.mark.parametrize("top,key,phase", [
    ("계획/화목동698-14", "화목동698-14", ""),
    ("##학장동 574-29/#허가", "학장동-574-29", "허가"),
    ("##학장동 574-29/#사용승인", "학장동-574-29", "사용승인"),
    ("##학장동 카페/허가", "학장동-카페", "허가"),
    ("###프로젝트/제로스", "제로스", ""),
    ("#감리/#부산여중 해체감리", "부산여중-해체감리", ""),
    ("용변/남천동 17-2", "남천동-17-2", ""),
    ("##캠핑장/인테리어 업체", "캠핑장", ""),
    ("5.설계방/양산주택/구조", "양산주택", "구조"),
    ("##작업중", "작업중", ""),
])
def test_canonical_project_merges_phase_folders(top, key, phase):
    result = canonical_project("P-x", top)
    assert (result["key"], result["phase"]) == (key, phase)


def test_canonical_project_without_census_uses_project_id():
    assert canonical_project("P-eval20")["key"] == "eval20"


def test_revisions_share_a_series_key_and_keep_their_dates():
    assert series_key("배치도_0611.dwg") == series_key("배치도_0626 - 복사본.dwg") == series_key("배치도(최종).dwg")
    assert series_key("A-101 1층 평면도_0611.dwg", "A-101") == series_key("A101.dwg", "A101".replace("A1", "A-1"))
    assert date_token("배치도_0626 - 복사본.dwg") == "0626"
    assert date_token("A-101_20240611.dwg") == "240611"
    assert date_token("평면도_1399.dwg") is None


def test_drawing_fields_reads_number_discipline_storey():
    f = drawing_fields("S-201 2층 구조평면도.dwg")
    assert f["sheet_number"] == "S-201" and f["discipline"] == "STRUCT" and f["storeys"] == ["2F"]


@pytest.mark.parametrize("raw,norm", [("H300*150*6.5*9", "H-300x150x6.5x9"), ("h-300X150X6.50X9", "H-300x150x6.5x9"),
                                      ("PL-12", "PL-12"), ("L-65x65x6", "L-65x65x6")])
def test_canonical_section(raw, norm):
    assert canonical_section(raw) == norm


def test_canonical_room_ignores_spacing_and_case():
    assert canonical_room("회의실 1") == canonical_room("회의실1") and canonical_room("PS") == canonical_room("ps")


@pytest.mark.parametrize("q,route", [
    ("학장동 카페 프로젝트 개요를 요약해줘", "summary"),
    ("H-300x150x6.5x9 단면은 어느 도면에 쓰였나?", "graph:section"),
    ("A-101 도면은 무엇인가?", "keyword:sheet"),
    ("2층 실 목록 알려줘", "graph:storey"),
    ("2층에 문은 몇 개야?", "graph:elements"),
    ("화장실은 어느 층에 있어?", "graph:room"),
    ("배치도 최신 버전은?", "graph:revision"),
    ("방수 상세 마감은 어떻게 되어 있나", "semantic"),
    ("옥상층평면도 관련 도면을 찾아줘", "graph:drawings"),
    ("용도변경개요 관련 도면을 찾아줘", "graph:drawings"),
])
def test_router(q, route):
    assert choose_route(link_static(q)) == route


def test_single_syllable_kind_words_need_token_boundaries():
    assert "Beam" in link_static("이 보에 쓰인 단면").kinds
    assert "Beam" not in link_static("정보 보고서").kinds
    assert "Door" in link_static("문은 몇 개").kinds
    assert "Door" not in link_static("문서 목록").kinds


def test_sheet_number_is_not_a_storey_or_section():
    linked = link_static("B1 층 H-300x150 단면")
    assert linked.sheet_numbers == [] and linked.storeys == ["B1"]


def test_llm_refuses_remote_endpoints_without_opt_in():
    assert is_local_endpoint("http://127.0.0.1:11434") and is_local_endpoint("http://host.docker.internal:11434")
    with pytest.raises(LLMError):
        LocalLLM(LLMConfig(url="https://api.example.com"))
    LocalLLM(LLMConfig(url="https://api.example.com", allow_remote=True))


def test_refusal_text_is_korean():
    assert "근거" in REFUSAL


def test_josa():
    from aec_intelligence.operational.graphrag.evaluate import josa

    assert josa("데크") == "데크는" and josa("화장실") == "화장실은" and josa("문", "이가") == "문이"


@pytest.mark.parametrize("q", ["오늘 부산 날씨 어때?", "주식 시장 전망을 알려줘"])
def test_out_of_scope_questions_are_gated_before_retrieval(q):
    from aec_intelligence.operational.graphrag.ask import unsupported_reason
    assert unsupported_reason(q, link_static(q)) is not None


@pytest.mark.parametrize("q", ["2층 회의실 면적이 얼마야?", "H-300x300 단면은 어느 도면에 쓰였어?", "A-101 도면은 뭐야?"])
def test_in_scope_questions_pass_the_static_gate(q):
    from aec_intelligence.operational.graphrag.ask import unsupported_reason
    assert unsupported_reason(q, link_static(q)) is None


def test_unsupported_attribute_needs_evidence_in_context():
    from aec_intelligence.operational.graphrag.ask import unsupported_by_context
    q = "학장동 카페 건축주의 전화번호를 알려줘"
    assert unsupported_by_context(q, ["프로젝트 학장동 카페 1F: 실 3개"]) is not None
    assert unsupported_by_context(q, ["건축주 홍길동 TEL 051-123-4567"]) is None
    assert unsupported_by_context("총 공사비는 얼마야?", ["실 '회의실': 면적 12.5㎡"]) is not None
    assert unsupported_by_context("회의실 면적이 얼마야?", ["실 '회의실': 면적 12.5㎡"]) is None


def _stub_ollama(handler_log, fail=False):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            handler_log.append((self.path, body))
            if fail:
                self.send_response(500)
                self.end_headers()
                return
            out = {"model": body["model"], "message": {"content": "<think>x</think>요약"}, "eval_count": 3}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_llm_warm_and_chat_share_num_ctx_and_keep_alive(monkeypatch):
    monkeypatch.setenv("AEC_LLM_KEEP_ALIVE", "3m")
    log = []
    srv = _stub_ollama(log)
    try:
        monkeypatch.setenv("AEC_LLM_URL", f"http://127.0.0.1:{srv.server_port}")
        llm = LocalLLM()
        assert llm.warm() >= 0
        assert llm.chat("s", "u")["text"] == "요약"
    finally:
        srv.shutdown()
    (warm_path, warm), (chat_path, chat) = log
    assert warm_path == "/api/generate" and warm["prompt"] == "" and chat_path == "/api/chat"
    assert warm["keep_alive"] == chat["keep_alive"] == "3m"
    assert warm["options"]["num_ctx"] == chat["options"]["num_ctx"]  # no reload between warm-up and chat


def test_summarize_stops_after_two_endpoint_failures():
    from aec_intelligence.operational.graphrag import communities

    class Conn:
        def __init__(self):
            self.updates = []

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def execute(self, sql, params=None):
            self.updates.append(sql.split()[0])
            rows = [{"id": i, "title": f"c{i}", "facts": ["f"]} for i in range(5)]

            class R:
                def fetchall(self_inner):
                    return rows
            return R()

        def commit(self):
            pass

        def rollback(self):
            pass

    class DB:
        conn = Conn()

        def connect(self, **kw):
            return self.conn

    class DownLLM:
        model = "m"
        calls = 0

        def warm(self):
            return 0.1

        def chat(self, *a, **k):
            DownLLM.calls += 1
            raise LLMError("LLM endpoint failed: timed out")

    out = communities.summarize(DB(), DownLLM())
    assert DownLLM.calls == 2 and out["failed"] == 1 and "timed out" in out["aborted"]

    class NoLoad(DownLLM):
        def warm(self):
            raise LLMError("LLM model load failed: refused")

    out = communities.summarize(DB(), NoLoad())
    assert out["pending"] == 5 and out["summarized"] == 0 and "load failed" in out["aborted"]
