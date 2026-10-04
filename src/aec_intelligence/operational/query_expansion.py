"""Bilingual query expansion: English questions against Korean drawings.

Drawing text is overwhelmingly Korean, so an English query has almost no lexical overlap and leans
entirely on the cross-lingual vector stage (search_eval: en MRR ~0.38 vs ko ~0.93). Expansion turns the
English query into Korean search terms before parsing:

* ``glossary`` (default): a small curated table of generic AEC terms (``AEC_GLOSSARY``: disciplines,
  drawing types, building parts, MEP systems, rooms, ordinals/storeys). No project data; public terms only.
  Longest phrase wins ("floor plan" before "floor"); English stop words are dropped; codes such as
  ``M-357`` or ``H-300x300`` are kept as they are.
* ``llm`` (opt-in, ``AEC_QUERY_EXPANSION=llm``): the local LLM (Ollama, never a cloud model) translates
  what the glossary did not cover. Translations are cached in-process and, with
  ``AEC_QUERY_TRANSLATION_CACHE=<file.json>``, on disk; an LLM error falls back to the glossary.
* ``off``: no expansion.

The lexical stage then searches the Korean terms (and the storey filter applies: "second floor" -> 2층),
while the vector stage embeds "<english> / <korean>" (bge-m3 is multilingual, both halves help).
Korean (or mixed Hangul) queries are returned unchanged (``None``).
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

# Generic AEC vocabulary only (no project, client, address or drawing names: the repository is public).
AEC_GLOSSARY: dict[str, str] = {
    # disciplines / documents
    "architectural": "건축", "architecture": "건축", "structural": "구조", "structure": "구조",
    "mechanical": "기계", "electrical": "전기", "electric": "전기", "plumbing": "위생 배관",
    "civil": "토목", "landscape": "조경", "landscaping": "조경", "fire protection": "소방", "fire fighting": "소방",
    "fire": "소방", "telecom": "통신", "telecommunication": "통신", "communication": "통신",
    "interior": "실내", "hvac": "공조 냉난방", "mep": "설비", "utilities": "설비",
    "drawing": "도면", "drawings": "도면", "sheet": "도면", "plan": "평면", "floor plan": "평면도",
    "site plan": "배치도", "layout plan": "배치도", "elevation": "입면도", "elevations": "입면도",
    "section": "단면도", "sections": "단면도", "cross section": "단면도", "detail": "상세", "details": "상세",
    "detail drawing": "상세도", "schedule": "일람표", "legend": "범례", "diagram": "계통도",
    "riser diagram": "계통도", "specification": "시방서", "specifications": "시방서", "notes": "사항",
    "general notes": "일반 사항", "general": "일반", "calculation": "계산서", "calculations": "계산서",
    "report": "보고서", "review": "검토", "index": "목록", "drawing list": "도면 목록", "title block": "도곽",
    "scale": "축척", "revision": "개정", "framing plan": "구조 평면도", "framing": "골조", "planning": "계획",
    "design": "설계", "construction": "공사", "existing": "기존", "demolition": "철거", "permit": "허가",
    # storeys / levels
    "basement": "지하", "ground floor": "1층", "first floor": "1층", "second floor": "2층",
    "third floor": "3층", "fourth floor": "4층", "fifth floor": "5층", "sixth floor": "6층",
    "first basement level": "지하1층", "second basement level": "지하2층", "third basement level": "지하3층",
    "first basement": "지하1층", "second basement": "지하2층", "third basement": "지하3층",
    "roof": "지붕", "rooftop": "지붕", "roof plan": "지붕 평면도", "penthouse": "옥탑", "floor": "층",
    "level": "층", "storey": "층", "story": "층", "mezzanine": "중층", "attic": "다락",
    # building parts / structure
    "wall": "벽", "walls": "벽", "column": "기둥", "columns": "기둥", "beam": "보", "beams": "보",
    "girder": "보", "slab": "슬래브", "foundation": "기초", "footing": "기초", "pile": "파일", "piles": "파일",
    "retaining wall": "옹벽", "stair": "계단", "stairs": "계단", "staircase": "계단", "ramp": "경사로",
    "elevator": "승강기", "lift": "승강기", "escalator": "에스컬레이터", "door": "문", "doors": "문",
    "window": "창호", "windows": "창호", "curtain wall": "커튼월", "ceiling": "천장", "partition": "칸막이",
    "parapet": "파라펫", "canopy": "캐노피", "balcony": "발코니", "corridor": "복도", "lobby": "로비",
    "reinforcement": "배근", "rebar": "철근", "reinforcing bar": "철근", "concrete": "콘크리트",
    "steel": "철골", "steel structure": "철골 구조", "steel beam": "철골 보", "connection": "접합",
    "bolt": "볼트", "welding": "용접", "precast": "프리캐스트", "masonry": "조적", "brick": "벽돌",
    "waterproofing": "방수", "insulation": "단열", "thermal insulation": "단열", "finish": "마감",
    "finishes": "마감", "finish schedule": "마감표", "tile": "타일", "paint": "도장", "joint": "줄눈",
    "thermal transmittance": "열관류율", "u-value": "열관류율", "fire resistance": "내화", "fireproofing": "내화",
    "earthwork": "토공", "excavation": "굴착 흙막이", "shoring": "흙막이", "earth retaining": "흙막이",
    "monitoring": "계측", "instruments": "계측기", "instrumentation": "계측", "survey": "측량",
    "drainage": "배수", "paving": "포장", "road": "도로", "parking": "주차", "parking lot": "주차장",
    # MEP
    "duct": "덕트", "ducts": "덕트", "ductwork": "덕트", "exhaust": "배기", "ventilation": "환기",
    "supply air": "급기", "return air": "환기", "air conditioning": "냉난방", "heating": "난방",
    "cooling": "냉방", "heating and cooling": "냉난방", "air handling unit": "공조기", "ahu": "공조기",
    "fan": "팬", "boiler": "보일러", "chiller": "냉동기", "pump": "펌프", "piping": "배관", "pipe": "배관",
    "pipes": "배관", "gas": "가스", "gas piping": "가스 배관", "water supply": "급수", "hot water": "급탕",
    "sewage": "오수", "wastewater": "오수", "storm water": "우수", "rainwater": "우수", "sprinkler": "스프링클러",
    "hydrant": "소화전", "fire alarm": "자동화재탐지", "smoke": "제연", "smoke control": "제연",
    "lighting": "조명", "light": "조명", "power": "전력", "outlet": "콘센트", "receptacle": "콘센트",
    "panel": "분전반", "panelboard": "분전반", "switchboard": "배전반", "transformer": "변압기",
    "generator": "발전기", "feeder": "간선", "main feeder": "간선", "distribution": "설비 계통",
    "grounding": "접지", "earthing": "접지", "lightning protection": "피뢰", "cable": "선로", "cables": "선로",
    "cable routing": "선로", "wiring": "배선", "conduit": "전선관", "cable tray": "케이블트레이",
    "cctv": "CCTV", "security": "보안", "network": "네트워크", "telephone": "전화", "tv": "TV",
    "solar": "태양광", "photovoltaic": "태양광", "heat pump": "히트펌프", "meter": "계량기",
    # spaces / building types
    "room": "실", "rooms": "실", "space": "실", "office": "사무실", "offices": "사무실",
    "laboratory": "실험실", "lab": "실험실", "restroom": "화장실", "toilet": "화장실", "bathroom": "욕실",
    "kitchen": "주방", "living room": "거실", "bedroom": "침실", "dining room": "식당", "cafeteria": "식당",
    "storage": "창고", "warehouse": "창고", "machine room": "기계실", "mechanical room": "기계실",
    "electrical room": "전기실", "pump room": "펌프실", "boiler room": "보일러실", "meeting room": "회의실",
    "conference room": "회의실", "classroom": "교실", "ward": "병실", "lounge": "휴게실", "entrance": "현관",
    "garage": "주차장", "hospital": "병원", "school": "학교", "apartment": "아파트", "house": "주택",
    "housing": "주택", "residential": "주거", "factory": "공장", "plant": "공장", "church": "교회",
    "hotel": "호텔", "library": "도서관", "gym": "체육관", "gymnasium": "체육관", "museum": "박물관",
    "dormitory": "기숙사", "nursery": "어린이집", "retail": "근린생활시설", "shop": "상가",
}
_STOP = {"a", "an", "the", "of", "on", "in", "at", "for", "to", "and", "or", "with", "by", "from", "is", "are",
         "what", "which", "where", "show", "me", "find", "list", "all", "please", "about", "its", "their",
         "this", "that", "these", "those", "level", "drawing", "drawings"}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10}
_HANGUL = re.compile(r"[가-힣]")
_CODE = re.compile(r"^[A-Za-z]{1,3}-?\d[\w\-x×.]*$|^\d")
_MAX_PHRASE = 4


@dataclass(frozen=True)
class Expansion:
    lexical_text: str
    embed_text: str
    method: str
    unknown: tuple[str, ...] = ()


def is_english(query: str) -> bool:
    return not _HANGUL.search(query) and len(re.findall(r"[A-Za-z]", query)) >= 3


def glossary_translate(query: str) -> tuple[list[str], list[str]]:
    """(korean terms, unknown english words) for an English query; codes are kept as terms."""
    words = re.findall(r"[A-Za-z]{1,3}-?\d[\w\-×.]*|[A-Za-z][A-Za-z\-']*|\d[\w\-×.]*", query)
    low = [w.lower() for w in words]
    out: list[str] = []
    unknown: list[str] = []
    i = 0
    while i < len(words):
        # "2nd floor" / "floor 3" / "level B2"
        m = re.fullmatch(r"(\d+)(st|nd|rd|th)?", low[i])
        if m and i + 1 < len(low) and low[i + 1] in ("floor", "level", "storey", "story", "f"):
            out.append(f"{int(m.group(1))}층")
            i += 2
            continue
        if low[i] in ("floor", "level", "storey", "story") and i + 1 < len(low) and low[i + 1].isdigit():
            out.append(f"{int(low[i + 1])}층")
            i += 2
            continue
        for n in range(min(_MAX_PHRASE, len(words) - i), 0, -1):
            phrase = " ".join(low[i:i + n])
            if phrase in AEC_GLOSSARY:
                out.extend(AEC_GLOSSARY[phrase].split())
                i += n
                break
        else:
            w = low[i]
            if w in _ORDINALS and i + 1 < len(low) and low[i + 1] in ("floor", "storey", "story"):
                out.append(f"{_ORDINALS[w]}층")
                i += 2
                continue
            if _CODE.match(words[i]):
                out.append(words[i])
            elif w not in _STOP and w not in _ORDINALS and len(w) > 1:
                unknown.append(words[i])
            i += 1
    return list(dict.fromkeys(out)), unknown


_CACHE: dict[str, str] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_LOADED: set[str] = set()
TRANSLATE_SYSTEM = (
    "Translate the English search query about architectural/engineering drawings into short Korean "
    "construction terms as they appear on Korean drawings (e.g. 'restroom exhaust duct' -> '화장실 배기 덕트'). "
    "Output only the Korean terms separated by spaces, no explanation."
)


def _cache_file() -> Path | None:
    raw = os.getenv("AEC_QUERY_TRANSLATION_CACHE", "").strip()
    return Path(raw) if raw else None


def _load_cache() -> None:
    path = _cache_file()
    if path is None or str(path) in _CACHE_LOADED:
        return
    _CACHE_LOADED.add(str(path))
    try:
        _CACHE.update(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def _save_cache() -> None:
    path = _cache_file()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_CACHE, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def llm_translate(query: str, llm=None) -> str | None:
    """Korean terms for ``query`` from the local LLM (cached); None when the LLM is unavailable."""
    key = query.strip().lower()
    with _CACHE_LOCK:
        _load_cache()
        if key in _CACHE:
            return _CACHE[key]
    try:
        if llm is None:
            from .graphrag.llm import LocalLLM

            llm = LocalLLM()
        text = llm.chat(TRANSLATE_SYSTEM, query, max_tokens=60)["text"]
    except Exception:  # noqa: BLE001 - expansion is optional; the glossary result is used instead
        return None
    text = " ".join(t for t in re.split(r"[\s,;/]+", text) if _HANGUL.search(t) or _CODE.match(t))[:120]
    if not text:
        return None
    with _CACHE_LOCK:
        _CACHE[key] = text
        _save_cache()
    return text


def expand_query(query: str, mode: str | None = None, llm=None) -> Expansion | None:
    mode = (mode or os.getenv("AEC_QUERY_EXPANSION") or "glossary").strip().lower()
    if mode in ("off", "0", "none", "false") or not is_english(query):
        return None
    terms, unknown = glossary_translate(query)
    method = "glossary"
    if mode == "llm" and unknown:
        translated = llm_translate(query, llm)
        if translated:
            terms = list(dict.fromkeys(terms + translated.split()))
            method = "glossary+llm"
    if not terms:
        return None
    korean = " ".join(terms)
    return Expansion(lexical_text=korean, embed_text=f"{query} / {korean}", method=method, unknown=tuple(unknown))
