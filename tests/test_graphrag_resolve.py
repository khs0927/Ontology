"""Phase 4 entity resolution and query routing rules (pure functions, no database)."""

import pytest

from aec_intelligence.operational.graphrag.ask import REFUSAL, choose_route, link_static
from aec_intelligence.operational.graphrag.llm import LLMError, LLMConfig, LocalLLM, is_local_endpoint
from aec_intelligence.operational.graphrag.resolve import (
    canonical_project,
    canonical_room,
    canonical_section,
    container_folders,
    date_token,
    drawing_fields,
    series_key,
)

TOPS = ["계획/화목동698-14", "계획/다른현장", "##학장동 574-29/#허가", "##학장동 574-29/#사용승인",
        "##학장동 카페/허가", "###프로젝트/제로스", "#감리/#부산여중 해체감리", "##작업중"]


def test_container_folders_need_two_non_phase_children():
    assert container_folders(TOPS) == {"계획"}


@pytest.mark.parametrize("top,key,phase", [
    ("계획/화목동698-14", "화목동698-14", ""),
    ("##학장동 574-29/#허가", "학장동-574-29", "허가"),
    ("##학장동 574-29/#사용승인", "학장동-574-29", "사용승인"),
    ("##학장동 카페/허가", "학장동-카페", "허가"),
    ("###프로젝트/제로스", "제로스", ""),
    ("#감리/#부산여중 해체감리", "부산여중-해체감리", ""),
])
def test_canonical_project_merges_phase_folders(top, key, phase):
    result = canonical_project("P-x", top, container_folders(TOPS))
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
