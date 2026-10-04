"""Fast refusal budget (Graph RAG semantic leg), English query expansion, perf tools."""

import json
from types import SimpleNamespace

import pytest

from aec_intelligence.operational import query_expansion as qe
from aec_intelligence.operational.graphrag import ask
from aec_intelligence.operational.perf import load_cases, parse_worker_log, search_eval, summarize


# ---------------------------------------------------------------- B: fast refusal
def test_asked_attribute_only_for_evidence_attributes():
    assert ask.asked_attribute("A 프로젝트의 총 공사비는 얼마야?")[0] == "공사비"
    assert ask.asked_attribute("건축주의 전화번호를 알려줘")[0] == "전화번호"
    assert ask.asked_attribute("오늘 날씨 어때?") is None  # refused statically, before retrieval
    assert ask.asked_attribute("2층 실 목록") is None


class StubSearch:
    def __init__(self, warn=False, hits=()):
        self.calls = []
        self.warn = warn
        self.hits = list(hits)

    def search(self, question, **kw):
        self.calls.append(kw)
        return SimpleNamespace(hits=self.hits, warnings=["search exceeded its 5 ms budget"] if self.warn else [])


class Conn:
    def execute(self, sql, params=None):
        return SimpleNamespace(fetchall=lambda: [{"alias": "p1"}, {"alias": "p2"}, {"alias": "p3"}])


def test_semantic_leg_gets_the_remaining_budget():
    stub = StubSearch()
    rag = ask.GraphRAG(db=None, settings=None, search_router=stub)
    rag._semantic(Conn(), "q", ["k"], budget_ms=5000)
    assert len(stub.calls) == 3 and all(0 < c["timeout_ms"] <= 5000 for c in stub.calls)
    assert rag._semantic_timed_out is False
    rag._semantic(Conn(), "q", None, budget_ms=None)  # no budget: one unscoped search, no timeout
    assert stub.calls[-1]["timeout_ms"] is None and stub.calls[-1]["project_id"] is None


def test_semantic_leg_reports_timeout_and_stops_when_budget_is_gone(monkeypatch):
    stub = StubSearch(warn=True)
    rag = ask.GraphRAG(db=None, settings=None, search_router=stub)
    rag._semantic(Conn(), "q", ["k"], budget_ms=1200)
    assert rag._semantic_timed_out is True
    clock = iter([0.0, 10.0, 10.0, 10.0])
    monkeypatch.setattr(ask.time, "monotonic", lambda: next(clock))
    stub.calls.clear()
    rag._semantic(Conn(), "q", ["k"], budget_ms=1200)
    assert stub.calls == [] and rag._semantic_timed_out is True


def test_probe_timeout_is_restored_for_the_rest_of_the_transaction():
    """SET LOCAL in a released savepoint would leak; the probe must put the old timeout back."""
    log = []

    class C:
        def execute(self, sql, params=None):
            log.append((sql, params))
            return SimpleNamespace(fetchone=lambda: {"v": "30s"})

    with ask._local_statement_timeout(C(), 1000):
        pass
    assert [p for s, p in log if "set_config" in s] == [("1000ms",), ("30s",)]


def test_budget_env(monkeypatch):
    monkeypatch.setenv("AEC_ASK_GATE_TIMEOUT_MS", "900")
    assert ask._budget_ms("AEC_ASK_GATE_TIMEOUT_MS", 1200) == 900
    monkeypatch.setenv("AEC_ASK_GATE_TIMEOUT_MS", "x")
    assert ask._budget_ms("AEC_ASK_GATE_TIMEOUT_MS", 1200) == 1200


def test_search_budget_exhausted_returns_empty_with_warning(monkeypatch):
    """Both the hybrid query and its lexical fallback time out -> no hits, a warning, no exception."""
    from contextlib import contextmanager

    from aec_intelligence.operational import search as search_mod

    class TimeoutConn:
        def transaction(self):
            @contextmanager
            def tx():
                yield
            return tx()

        def execute(self, sql, params=None):
            if "set_config" in sql or "current_setting" in sql:
                return SimpleNamespace(fetchall=lambda: [], fetchone=lambda: {"v": "30s"})
            raise RuntimeError("canceling statement due to statement timeout")

    class DB:
        @contextmanager
        def connect(self):
            yield TimeoutConn()

    monkeypatch.delenv("AEC_EMBEDDING_URL", raising=False)
    settings = SimpleNamespace(embedding_model="BAAI/bge-m3", embedding_url="")
    router = search_mod.SearchRouter(DB(), settings)
    res = router.search("방수 상세", timeout_ms=5)
    assert res.hits == [] and any("budget" in w for w in res.warnings)
    with pytest.raises(RuntimeError):  # without a budget the error still surfaces as before
        router.search("방수 상세")


# ---------------------------------------------------------------- C: bilingual expansion
@pytest.mark.parametrize("query, expected", [
    ("restroom exhaust ductwork", "화장실 배기 덕트"),
    ("gas piping on the second floor", "가스 배관 2층"),
    ("second basement level floor plan", "지하2층 평면도"),
    ("floor 3 toilet", "3층 화장실"),
    ("H-300x300 steel beam", "H-300x300 철골 보"),
])
def test_glossary_expansion(query, expected):
    exp = qe.expand_query(query, mode="glossary")
    assert exp.lexical_text == expected and exp.embed_text.startswith(query) and exp.method == "glossary"


def test_korean_and_off_are_unchanged():
    assert qe.expand_query("화장실 환기 덕트") is None
    assert qe.expand_query("2층 HVAC 배관") is None  # mixed Hangul: the user already wrote Korean terms
    assert qe.expand_query("restroom duct", mode="off") is None
    assert qe.expand_query("xyzzy plugh", mode="glossary") is None  # nothing known -> plain search


def test_glossary_has_no_private_looking_entries():
    for en, ko in qe.AEC_GLOSSARY.items():
        assert en == en.lower() and len(ko) <= 20 and not any(ch.isdigit() for ch in en)


def test_llm_translation_is_cached(monkeypatch, tmp_path):
    cache = tmp_path / "tr.json"
    monkeypatch.setenv("AEC_QUERY_TRANSLATION_CACHE", str(cache))
    monkeypatch.setattr(qe, "_CACHE", {})
    monkeypatch.setattr(qe, "_CACHE_LOADED", set())
    calls = []

    class LLM:
        def chat(self, system, user, max_tokens=60):
            calls.append(user)
            return {"text": "옥탑 물탱크, 설명 없음 tank"}

    exp = qe.expand_query("penthouse water tank", mode="llm", llm=LLM())
    assert exp.method == "glossary+llm" and "물탱크" in exp.lexical_text and "tank" not in exp.lexical_text
    assert json.loads(cache.read_text(encoding="utf-8"))["penthouse water tank"]
    qe.expand_query("penthouse water tank", mode="llm", llm=LLM())
    assert len(calls) == 1  # second call served from the cache

    class Down:
        def chat(self, *a, **k):
            raise OSError("ollama down")

    exp = qe.expand_query("penthouse cistern", mode="llm", llm=Down())
    assert exp.method == "glossary" and exp.lexical_text == "옥탑"


def test_search_uses_expansion_for_lexical_terms(monkeypatch):
    from aec_intelligence.operational import search as search_mod

    seen = {}
    real = search_mod.parse_query
    monkeypatch.setattr(search_mod, "parse_query", lambda q: seen.setdefault("q", q) and real(q))
    monkeypatch.delenv("AEC_EMBEDDING_URL", raising=False)

    class Stop(Exception):
        pass

    class DB:
        def connect(self):
            raise Stop

    router = search_mod.SearchRouter(DB(), SimpleNamespace(embedding_model="m", embedding_url=""))
    with pytest.raises(Stop):
        router.search("restroom exhaust ductwork")
    assert seen["q"] == "화장실 배기 덕트"


# ---------------------------------------------------------------- D: perf tools
def test_parse_worker_log_durations():
    lines = [
        "[w1] 2026-10-04 21:30:00,001 INFO Worker host-1 claimed job aaa on cad",
        "[w1] 2026-10-04 21:30:40,500 INFO Job aaa succeeded",
        "[w1] 2026-10-04 21:31:00,000 INFO Worker host-1 claimed job bbb on cad",
        "[w1] 2026-10-04 21:33:00,000 WARNING Job bbb: failed (timeout)",
        "[w1] 2026-10-04 21:34:00,000 INFO Job zzz succeeded",
    ]
    jobs = parse_worker_log(lines)
    assert [(j["job"], j["seconds"], j["result"]) for j in jobs] == [("aaa", 40.0, "succeeded"), ("bbb", 120.0, "failed")]


def test_cases_and_search_eval(tmp_path):
    f = tmp_path / "cases.json"
    f.write_text(json.dumps([{"q": "a", "expect": "D1", "set": "ko"}, ["b", "D2", "en"]]), encoding="utf-8")
    cases = load_cases(f)
    assert [c["set"] for c in cases] == ["ko", "en"]

    def hit(doc):
        return SimpleNamespace(citation=SimpleNamespace(document_id=doc))

    class Router:
        def search(self, q, **kw):
            docs = {"a": ["D1"], "b": ["X", "Y", "D2"]}.get(q, [])
            return SimpleNamespace(hits=[hit(d) for d in docs], warnings=[])

    res = search_eval(Router(), cases)
    assert res["sets"]["ko"]["mrr"] == 1.0 and res["sets"]["en"]["mrr"] == round(1 / 3, 3)
    assert summarize([])["n"] == 0
