"""Evidence-based CAD semantic classification.

Signals are read from the most specific source first: the (effective) block
name of an INSERT, then its attribute tags/values, then the layer name. Text
entities are classified from their layer only; their content feeds the room,
mark, steel-section, detail-title and sheet-category helpers below instead of
turning a note such as "방화문 상세" into a door.

Rule tokens (second element of each ``RULES`` row):

* ``"door"``     ASCII word, substring match (legacy behaviour, long words only)
* ``"=wc"``      exact segment match (segments are split on separators)
* ``"^col"``     segment prefix
* ``"#sd\\d*"``   full-segment regular expression
* ``"벽"``       Korean, substring match after removing ``KOREAN_STOPWORDS``
* ``"=보"``      Korean, exact segment
* ``"$창"``      Korean, segment suffix (고정창, 미서기창 ...)
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from .cair import CAIRObject, Classification, Provenance, SourceRef, stable_object_id
from .dxf import NormalizedCADEntity


# Ordered so that specific labels win over generic ones (a "STAIR" layer is not a slab,
# a "WINDOW" block on a wall layer is a window).
RULES: list[tuple[str, tuple[str, ...], str, float]] = [
    ("Furniture", ("furniture", "furn", "toilet", "lavatory", "bathtub", "urinal", "shower", "sofa", "=sink", "=wc",
                   "=bed", "=desk", "=chair", "=lav", "=ub", "가구", "변기", "소변기", "세면대", "욕조", "싱크",
                   "샤워", "침대", "소파", "책상", "의자", "위생기구", "수전"), "name indicates furniture/sanitary fixture", 0.86),
    ("Elevator", ("elevator", "=elev", "=lift", "#ev\\d{0,2}", "엘리베이터", "승강기"), "name indicates elevator", 0.86),
    ("Stair", ("stair", "계단"), "name indicates stair", 0.87),
    ("Window", ("window", "a-wi", "w-window", "#(?:aw|pw|sw|ssw|alw|ww|fw)\\d{1,3}[a-z]?", "#w\\d{1,2}[a-z]?",
                "=창", "$창", "창호", "창문", "창틀", "고정창", "미서기", "미닫이창", "프로젝트창", "커튼월"),
     "name indicates window", 0.90),
    ("Door", ("door", "a-dr", "d-door", "#(?:ssd|asd|sd|wd|fd|ad|ald|gd|pd|hd)\\d{1,3}[a-z]?", "#d\\d{1,2}[a-z]?",
              "=문", "#(?!창문$)[가-힣]*문", "출입문", "방화문", "현관문", "여닫이", "미닫이", "자동문", "회전문", "문틀", "문짝", "도어"),
     "name indicates door", 0.90),
    ("Column", ("column", "^col", "a-col", "#(?:c|sc|tc)\\d{1,2}[a-z]?", "기둥"), "name indicates column", 0.88),
    ("Beam", ("beam", "girder", "s-beam", "=보", "큰보", "작은보", "대들보", "철골보", "거더"), "name indicates beam", 0.88),
    ("Wall", ("wall", "partition", "a-wal", "w-wall", "벽", "조적", "칸막이"), "name indicates wall", 0.91),
    ("Slab", ("slab", "floor", "s-slab", "바닥", "슬래브", "슬라브"), "name indicates slab", 0.86),
    ("Grid", ("grid", "axis", "a-grid", "그리드", "중심선", "통심"), "name indicates grid", 0.84),
]

# Korean words that contain a short element token but mean something else.
# They are blanked out before Korean substring/segment matching (창고 = storage room, 문자 = text ...).
KOREAN_STOPWORDS = ("창고", "창립", "문자", "문서", "문구", "문양", "문화", "주문", "질문", "전문", "방문객",
                    "정보", "경보", "홍보", "보일러", "보호", "보도", "보강", "보온", "보수", "확보", "벽지")
_STOP_RE = re.compile("|".join(sorted(map(re.escape, KOREAN_STOPWORDS), key=len, reverse=True)))
_EXCLUDED_PREFIXES = ("color", "colour")
_SPLIT_RE = re.compile(r"[^0-9a-z가-힣]+")


def _segments(value: str) -> list[str]:
    """Separator-delimited segments plus joined mark forms ('sd-1' also yields 'sd1')."""
    segments = [segment for segment in _SPLIT_RE.split(value) if segment]
    joined = [a + b for a, b in zip(segments, segments[1:]) if a.isalpha() and a.isascii() and len(a) <= 3 and b.isdigit()]
    return segments + joined


class _Evidence:
    """A lower-cased evidence string with its segmentations computed once."""

    __slots__ = ("value", "korean", "segments", "korean_segments")

    def __init__(self, value: str):
        self.value = value
        self.korean = _STOP_RE.sub(" ", value)
        self.segments = _segments(value)
        self.korean_segments = _segments(self.korean)


@lru_cache(maxsize=None)
def _compiled(body: str) -> re.Pattern[str]:
    return re.compile(body)


def _token_matches(evidence: _Evidence, token: str) -> bool:
    if token[0] in "=^#$":
        kind, body = token[0], token[1:]
        segments = evidence.segments if body.isascii() else evidence.korean_segments
        if kind == "=":
            return body in segments
        if kind == "^":
            return any(s.startswith(body) and not s.startswith(_EXCLUDED_PREFIXES) for s in segments)
        if kind == "$":
            return any(s.endswith(body) for s in segments)
        pattern = _compiled(body)
        return any(pattern.fullmatch(s) for s in segments)
    if token.isascii():
        return token in evidence.value
    return token in evidence.korean


def _contains_token(value: str, token: str) -> bool:
    """Match one rule token against an already lower-cased evidence string."""
    return _token_matches(_Evidence(value), token)


def _match(value: str) -> tuple[str, str, float, str] | None:
    if not value.strip():
        return None
    evidence = _Evidence(value)
    for label, tokens, reason, base_confidence in RULES:
        for token in tokens:
            if _token_matches(evidence, token):
                return label, reason, base_confidence, token.lstrip("=^#$")
    return None


def classify(entity: NormalizedCADEntity) -> tuple[str, Classification]:
    layer = entity.layer.lower()
    props = entity.properties
    sources: list[tuple[str, str, float]] = []
    if entity.entity_type == "INSERT":
        for name in dict.fromkeys((props.get("effective_name"), props.get("effective_block_name"), props.get("block_name"))):
            # Anonymous names (*U12 dynamic representations, *D3 dimension blocks) carry no meaning.
            if name and not str(name).startswith("*"):
                sources.append(("block", str(name).lower(), 0.02))
        attributes = props.get("attributes") or {}
        if attributes:
            values = " ".join(f"{tag} {value}" for tag, value in attributes.items())
            sources.append(("attribute", values.lower(), 0.0))
    if entity.entity_type not in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
        # A tag such as "SD1" or a note "방화문 상세" on a door layer annotates a door; it is not one.
        sources.append(("layer", layer, 0.0))
    for source, value, bonus in sources:
        found = _match(value)
        if found:
            label, reason, base_confidence, token = found
            confidence = round(min(base_confidence + bonus, 0.95), 2)
            evidence = (f"{source} {reason}: {token}", f"entity_type={entity.entity_type}", f"layer={entity.layer}")
            return label, Classification(label, confidence, "hybrid_rules", evidence, _state(confidence))
    if entity.entity_type in {"TEXT", "MTEXT"}:
        confidence = 0.78
        return "Annotation", Classification("Annotation", confidence, "entity_type_rule", ("entity_type=text", f"layer={entity.layer}"), _state(confidence))
    if entity.entity_type in {"DIMENSION", "LEADER", "MLEADER"}:
        confidence = 0.82
        return "Dimension", Classification("Dimension", confidence, "entity_type_rule", (f"entity_type={entity.entity_type}",), _state(confidence))
    confidence = 0.35
    return "CADEntity", Classification("CADEntity", confidence, "fallback", (f"entity_type={entity.entity_type}", f"layer={entity.layer}"), _state(confidence))


def _state(confidence: float) -> str:
    if confidence > 0.95:
        return "AUTO_ACCEPT"
    if confidence >= 0.75:
        return "ACCEPT_WITH_WARNING"
    return "REQUIRE_VALIDATION"


# --------------------------------------------------------------------------- text semantics

_MARK_RE = re.compile(
    r"^(?:(?P<door>SSD|ASD|SD|WD|FD|AD|ALD|GD|PD|HD|D)|(?P<window>SSW|AW|PW|SW|ALW|WW|FW|W)|(?P<column>SC|TC|C))"
    r"-?(?P<num>\d{1,3}[A-Z]?)$")


def element_mark(text: str) -> dict[str, str] | None:
    """Door/window/column tag such as 'SD1', 'AW-03', 'D12', 'C1' -> {'mark': 'SD1', 'mark_kind': 'Door'}."""
    compact = re.sub(r"\s+", "", str(text)).upper()
    found = _MARK_RE.match(compact)
    if not found:
        return None
    kind = "Door" if found["door"] else "Window" if found["window"] else "Column"
    return {"mark": compact.replace("-", ""), "mark_kind": kind}


ROOM_NAMES: tuple[str, ...] = (
    # Korean
    "엘리베이터홀", "엘리베이터 홀", "e/v홀", "ev홀", "다용도실", "드레스룸", "파우더룸", "계단실", "화장실", "욕실", "샤워실",
    "거실", "침실", "안방", "작은방", "주방", "부엌", "식당", "현관", "발코니", "베란다", "복도", "로비", "홀", "사무실",
    "회의실", "응접실", "창고", "기계실", "전기실", "발전기실", "펌프실", "방재실", "관리실", "경비실", "주차장", "서재",
    "공부방", "아이방", "알파룸", "팬트리", "세탁실", "보일러실", "실외기실", "대피공간", "기도실", "탕비실", "휴게실",
    "강의실", "교실", "도서실", "열람실", "체력단련실", "다목적실", "옥탑", "테라스", "데크", "전실", "부속실", "통신실",
    "준비실", "락커룸", "탈의실", "수유실", "매점", "점포", "근린생활시설", "판매시설", "학원", "의원", "약국", "식품창고",
    # English
    "living room", "living", "bedroom", "master bedroom", "kitchen", "dining", "dining room", "toilet", "restroom",
    "bathroom", "bath", "entrance", "foyer", "balcony", "utility", "dress room", "dressroom", "stair hall", "corridor",
    "hall", "lobby", "office", "meeting room", "conference room", "storage", "store", "machine room", "mechanical room",
    "electrical room", "elec. room", "parking", "garage", "elevator hall", "ev hall", "pantry", "laundry", "study",
    "terrace", "deck", "lounge",
)
_ROOM_ALT = "|".join(sorted((re.escape(name) for name in ROOM_NAMES), key=len, reverse=True))
_ROOM_RE = re.compile(
    rf"^\s*(?:(?P<num1>[A-Z]?\d{{1,4}}[A-Z]?)(?:호|호실)?\s*[-.:)]?\s*)?(?P<name>(?:\d\s*)?(?:{_ROOM_ALT})(?:\s*\d{{1,2}}(?![\d.,]))?)"
    rf"(?:\s*[(\[]?\s*(?P<num2>[A-Z]?\d{{1,4}}[A-Z]?)\s*(?:호|호실)\s*[)\]]?)?"
    rf"(?:\s*[(\[]?\s*(?P<area>\d{{1,5}}(?:[.,]\d{{1,3}})?)\s*(?:㎡|m2|m²|sqm|m\^2)\s*[)\]]?)?"
    rf"(?:\s*\(?(?P<pyeong>\d{{1,4}}(?:\.\d{{1,2}})?)\s*평\)?)?\s*$",
    re.IGNORECASE)


def room_from_text(text: str) -> dict[str, Any] | None:
    """Parse '거실', '101호 회의실', '침실1 12.5㎡', 'LIVING ROOM (24.3 m2)' into room properties."""
    lines = [line.strip() for line in re.split(r"[\r\n]+|\\P", str(text)) if line.strip()]
    if not lines or len(lines) > 3 or len(lines[0]) > 40:
        return None
    found = _ROOM_RE.match(lines[0])
    if not found:
        return None
    result: dict[str, Any] = {"roomName": re.sub(r"\s+", " ", found["name"]).strip()}
    number = found["num1"] or found["num2"]
    if number:
        result["roomNumber"] = number
    area = found["area"]
    for extra in lines[1:]:
        area_match = re.search(r"(\d{1,5}(?:[.,]\d{1,3})?)\s*(?:㎡|m2|m²|sqm)", extra, re.IGNORECASE)
        number_match = re.search(r"(\d{1,4}[A-Z]?)\s*(?:호|호실)", extra)
        if area_match and not area:
            area = area_match.group(1)
        elif number_match and "roomNumber" not in result:
            result["roomNumber"] = number_match.group(1)
        elif not (area_match or number_match):
            return None
    if area:
        value = float(area.replace(",", "."))
        if value > 0:
            result["area"] = value
    return result


_STEEL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<shape>H|I|BH|C|L|T|PIPE|P|□|ㅁ|SHS|RHS|FB|PL)\s*-?\s*"
    r"(?P<dims>\d+(?:\.\d+)?(?:\s*[xX×*]\s*\d+(?:\.\d+)?){0,4})"
    r"(?:\s*[tT]\s*(?P<t>\d+(?:\.\d+)?))?")
_STEEL_MIN_DIMS = {"H": 2, "I": 2, "BH": 2, "C": 2, "L": 2, "T": 2, "□": 2, "ㅁ": 2, "SHS": 2, "RHS": 2, "PIPE": 1,
                   "P": 2, "FB": 2, "PL": 1}


def steel_sections(text: str) -> list[dict[str, Any]]:
    """Find steel section designations such as H-400x200x8x13 / H400*200*8*13 / □-150x150x6 / PL-12."""
    results: list[dict[str, Any]] = []
    for found in _STEEL_RE.finditer(str(text).upper().replace("Ø", "").replace("Φ", "")):
        shape = found["shape"]
        dims = [d for d in re.split(r"\s*[X×*]\s*", found["dims"].strip()) if d]
        if found["t"]:
            dims.append(found["t"])
        if len(dims) < _STEEL_MIN_DIMS[shape]:
            continue
        if shape in {"□", "ㅁ"}:
            shape = "SHS" if len(dims) >= 2 and dims[0] == dims[1] else "RHS"
        elif shape == "P":
            shape = "PIPE"
        designation = f"{shape}-{'x'.join(dims)}"
        results.append({"sectionDesignation": designation, "shape": shape,
                        "dimensions": [float(d) for d in dims], "source_text": found.group(0).strip()})
    return results


_DETAIL_RE = re.compile(r"(상세도|상세|디테일|\bDETAILS?\b|\bDET\.|\bSECTION\s+[A-Z0-9]{1,3}\s*-\s*[A-Z0-9]{1,3}\b|단면\s*상세|"
                        r"단면도|\bSECTION\b|부분\s*확대)", re.IGNORECASE)
_STRONG_DETAIL_RE = re.compile(r"(상세도|\bDETAIL\b|\bSECTION\s+[A-Z0-9]{1,3}\s*-\s*[A-Z0-9]{1,3}\b|단면\s*상세|상세\s*\d|"
                               r"[A-Z0-9가-힣]+\s*상세$|단면도$)", re.IGNORECASE)


def detail_title(text: str) -> dict[str, Any] | None:
    """Detail/section callout title. ``strong`` marks titles that delimit a detail region."""
    first = next((line.strip() for line in re.split(r"[\r\n]+|\\P", str(text)) if line.strip()), "")
    if not first or len(first) > 60 or not _DETAIL_RE.search(first):
        return None
    if re.search(r"참조|참고|\bREFER|\bSEE\b|\bREF\.", first, re.IGNORECASE):
        return None
    strong = bool(_STRONG_DETAIL_RE.search(first)) and len(first) <= 40 and not first.endswith((".", "다"))
    category = "단면도" if re.search(r"SECTION|단면도", first, re.IGNORECASE) and not re.search(r"상세|DETAIL", first, re.IGNORECASE) else "상세도"
    return {"detail_title": first, "strong": strong, "drawing_category": category}


DRAWING_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("철골상세도", "steel_detail", (r"철골.*상세", r"STEEL.*DETAIL", r"CONNECTION\s+DETAIL", r"접합.*상세")),
    ("구조평면도", "structural_plan", (r"구조.*평면", r"STRUCTURAL.*PLAN", r"FRAMING\s+PLAN", r"(보|기둥|슬래브)\s*배근", r"골조.*평면")),
    ("창호도", "door_window", (r"창호", r"WINDOW\s+(SCHEDULE|ELEVATION|DETAIL)", r"DOOR\s*(&|AND)?\s*WINDOW")),
    ("일람표", "schedule", (r"일람", r"SCHEDULE", r"리스트표", r"부재표", r"마감표")),
    ("표지/목록", "cover_index", (r"표지", r"목록", r"COVER", r"DRAWING\s+(LIST|INDEX)", r"\bINDEX\b")),
    ("상세도", "detail", (r"상세", r"DETAIL", r"\bDET\b", r"확대")),
    ("단면도", "section", (r"단면", r"SECTION")),
    ("입면도", "elevation", (r"입면", r"ELEVATION")),
    ("배치도", "site_plan", (r"배치", r"SITE\s*PLAN", r"LAYOUT\s+PLAN")),
    ("평면도", "plan", (r"평면", r"\bPLAN\b", r"FLOOR", r"\d+\s*층", r"\bB?\d+F\b")),
)


def drawing_category(*candidates: tuple[str, str]) -> dict[str, str]:
    """First matching category over (source, text) candidates in priority order; '기타' when nothing matches."""
    for source, text in candidates:
        if not text:
            continue
        for ko, en, patterns in DRAWING_CATEGORIES:
            for pattern in patterns:
                if re.search(pattern, str(text), re.IGNORECASE):
                    return {"drawing_category": ko, "drawing_category_en": en,
                            "drawing_category_source": source, "drawing_category_evidence": str(text)[:120]}
    return {"drawing_category": "기타", "drawing_category_en": "other", "drawing_category_source": "none",
            "drawing_category_evidence": ""}


TITLE_BLOCK_KEYS: dict[str, tuple[str, ...]] = {
    "drawingNumber": ("DWG_NO", "DWGNO", "DRAWING_NO", "DRAWINGNO", "DRAWING_NUMBER", "DWG_NUMBER", "SHEET_NO", "SHEETNO",
                      "DNO", "도면번호", "도번", "시트번호"),
    "drawingTitle": ("TITLE", "DWG_TITLE", "DRAWING_TITLE", "DWG_NAME", "DRAWING_NAME", "SHEET_TITLE", "도면명", "도면제목", "제목"),
    "scale": ("SCALE", "축척", "SCALE_A1", "SCALE_A3"),
    "date": ("DATE", "일자", "날짜", "작성일"),
    "revisionLabel": ("REV", "REVISION", "REV_NO", "개정", "개정번호"),
    "projectName": ("PROJECT", "PROJECT_NAME", "PROJ_NAME", "공사명", "프로젝트", "사업명"),
}
_TITLE_LOOKUP = {re.sub(r"[\s._-]", "", key).upper(): field for field, keys in TITLE_BLOCK_KEYS.items() for key in keys}


def title_block_fields(attributes: dict[str, Any]) -> dict[str, str] | None:
    """Map INSERT attributes to title block fields; needs a drawing number or title plus one more field."""
    fields: dict[str, str] = {}
    for tag, value in (attributes or {}).items():
        field = _TITLE_LOOKUP.get(re.sub(r"[\s._-]", "", str(tag)).upper())
        if field and field not in fields and str(value).strip():
            fields[field] = str(value).strip()
    recognised = {_TITLE_LOOKUP.get(re.sub(r"[\s._-]", "", str(t)).upper()) for t in (attributes or {})} - {None}
    if not ({"drawingNumber", "drawingTitle"} & recognised) or len(recognised) < 2:
        return None
    return fields


def to_cair_object(
    entity: NormalizedCADEntity,
    project_id: str,
    source_file: str,
    source_hash: str,
    artifact_id: str | None = None,
    parser_name: str = "ezdxf",
    parser_version: str = "unknown",
    geometry_index_ref: str | None = None,
) -> CAIRObject:
    label, classification = classify(entity)
    object_id = stable_object_id(project_id, label, "DXF", entity.handle)
    source = SourceRef(source_file, "DXF", entity.handle, entity.layer, artifact_id)
    provenance = Provenance(source_file, entity.handle, "DXF", source_hash, parser_name, parser_version)
    return CAIRObject(
        id=object_id,
        project_id=project_id,
        type=label,
        source=source,
        geometry_ref=geometry_index_ref or f"aec://geometry/{project_id}/{entity.handle}",
        bbox=entity.bbox,
        placement=entity.geometry.get("location", {}),
        properties={"cad_entity_type": entity.entity_type, "layer": entity.layer, **entity.properties},
        classification=classification,
        provenance=provenance,
    )
