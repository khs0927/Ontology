import pytest

from aec_intelligence.operational.graphrag.ask import choose_route, link_static, unsupported_reason
from aec_intelligence.operational.graphrag.knowledge_pack import (
    KnowledgePackError,
    _communities,
    project_key,
    search_text,
    to_turtle,
    validate_pack,
)


def _pack():
    return {
        "schema": "aec-knowledge-pack/1", "pack": "drafting", "fingerprint": "abc",
        "nodes": [
            {"id": "kb:drafting", "type": "KnowledgePack", "name": "도면 작성 지식", "text": "작성 기준"},
            {"id": "kb:layer:WAL", "type": "Layer", "name": "WAL", "text": "벽 외곽선 레이어"},
            {"id": "kb:lesson:leader", "type": "Lesson", "name": "LEADER style 버그",
             "text": "문제: style을 문자 스타일로 해석\n해결: style 없이 재시도", "sources": ["scripts/x.py"]},
        ],
        "edges": [{"src": "kb:drafting", "predicate": "hasLesson", "dst": "kb:lesson:leader", "evidence": {}}],
        "aliases": [["layer", "WAL", "kb:layer:WAL"]],
    }


def test_validate_and_key():
    p = _pack()
    validate_pack(p)
    assert project_key(p) == "kb:drafting"
    assert "벽 외곽선" in search_text(p["nodes"][1])


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(schema="x"),
    lambda p: p["nodes"].append({"id": "kg:p:x", "type": "Project", "name": "x"}),  # must stay in kb: namespace
    lambda p: p["edges"].append({"src": "kb:drafting", "predicate": "x", "dst": "kb:missing"}),
    lambda p: p["aliases"].append(["a", "b", "kb:missing"]),
    lambda p: p["nodes"].append(dict(p["nodes"][1])),
])
def test_validate_rejects(mutate):
    p = _pack()
    mutate(p)
    with pytest.raises(KnowledgePackError):
        validate_pack(p)


def test_communities_one_per_text_node():
    comms = _communities(_pack(), "kb:drafting")
    assert comms[0]["level"] == 0 and "Layer 1개" in comms[0]["facts"]
    assert {c["title"] for c in comms[1:]} == {"[Layer] WAL", "[Lesson] LEADER style 버그"}


def test_turtle_is_owl():
    ttl = to_turtle(_pack())
    assert "kbo:Lesson a owl:Class" in ttl
    assert "kbo:hasLesson a owl:ObjectProperty" in ttl
    assert "kb:lesson_leader" in ttl and 'dcterms:source "scripts/x.py"' in ttl
    assert "\\n해결" in ttl  # newlines escaped inside literals


@pytest.mark.parametrize("q,route", [
    ("평면도 해치는 어떻게 그려?", "knowledge"),
    ("leader style 버그 해결 방법", "knowledge"),
    ("2층 문 몇 개야?", "graph:elements"),
])
def test_knowledge_route(q, route):
    linked = link_static(q)
    assert choose_route(linked) == route
    assert unsupported_reason(q, linked) is None


def test_knowledge_route_criteria_words():
    linked = link_static("외벽 단열재 표기 기준은?")
    assert choose_route(linked) == "knowledge"
