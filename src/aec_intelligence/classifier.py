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
                   "샤워", "침대", "소파", "책상", "의자", "위생기구", "수전", "실외기", "^eqpm", "equipment", "화장실"),
     "name indicates furniture/sanitary fixture/equipment", 0.86),
    # "ELEV" is deliberately absent: in AIA/KCS layer names (A-ELEV-*) it means elevation.
    ("Elevator", ("elevator", "=lift", "#ev\\d{1,2}", "엘리베이터", "승강기"), "name indicates elevator", 0.86),
    ("Stair", ("stair", "=strs", "계단"), "name indicates stair", 0.87),
    ("Bolt", ("=bolt", "^bolt", "s-bolt", "anchor", "볼트", "앵커"), "name indicates bolt", 0.84),
    ("Window", ("window", "a-wi", "w-window", "^win", "^glaz", "glazing", "#(?:aw|pw|sw|ssw|alw|ww|fw)\\d{1,3}[a-z]?", "#w\\d{1,2}[a-z]?",
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
    # Drafting symbols and annotation layers (section/detail marks, level marks, north arrows, schedule tables).
    ("Annotation", ("anno", "^symb", "=mark", "^tabl", "=note", "=text", "=level", "=lvl", "north", "=iden",
                    "방위", "레벨", "기호", "주석", "표기"), "name indicates annotation", 0.8),
]

# Bare abbreviations that are only trusted as a whole block name (an "SD" layer is a smoke detector).
BLOCK_ONLY_RULES: tuple[tuple[str, str], ...] = (
    ("Door", r"(sd|ad|wd|ssd|door)"),
    ("Window", r"(aw|pw|sw|ssw|alw|win)"),
)
# Whole-layer patterns tried only after RULES found nothing (drafting conventions without a
# self-describing element word). Lower confidence: they describe what a layer usually holds.
LAYER_FALLBACK_RULES: tuple[tuple[str, str, str, float], ...] = (
    # Elevation outline = the facade/wall silhouette (A-ELEV-OTLN, A-ELEV-OUTL).
    (r"(?:^|[-_])elev[-_](?:otln|outl|outline)(?:$|[-_])", "Wall", "elevation outline layer indicates wall silhouette", 0.76),
    # Ground line in elevations/sections is a reference line, not an element (A-ELEV-GRND, GL).
    (r"(?:^|[-_])(?:grnd|ground|gl|지반선|지반)(?:$|[-_])", "Annotation", "ground line layer indicates reference annotation", 0.76),
    # Steel profile outlines in a steel detail (S-STEEL, S-STL, 철골).
    (r"(?:^|[-_])(?:steel|stl|철골)(?:$|[-_])", "SteelSection", "steel layer indicates steel section profile", 0.76),
    # Generic material hatch without an element word (A-HATCH, A-PATT).
    (r"(?:^|[-_])(?:hatch|patt|pattern|해치)(?:$|[-_])", "BuildingElementProxy", "hatch layer indicates unspecified element fill", 0.6),
)
TITLE_BLOCK_TOKENS = ("=title", "^title", "titleblock", "도곽", "표제란", "=border", "타이틀")

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
    if entity.entity_type in {"DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION"}:
        confidence = 0.82
        return "Dimension", Classification("Dimension", confidence, "entity_type_rule", (f"entity_type={entity.entity_type}",), _state(confidence))
    if entity.entity_type in {"LEADER", "MLEADER", "MULTILEADER", "TOLERANCE"}:
        # A leader points at something with a note; it is an annotation, not a measured dimension.
        confidence = 0.82
        return "Annotation", Classification("Annotation", confidence, "entity_type_rule", (f"entity_type={entity.entity_type}",), _state(confidence))
    if entity.entity_type == "INSERT":
        title = _title_block(entity)
        if title:
            return "TitleBlock", Classification("TitleBlock", 0.9, "attribute_rules", (title, f"layer={entity.layer}"), _state(0.9))
        for source, value, _ in sources:
            if source != "block":
                continue
            for label, pattern in BLOCK_ONLY_RULES:
                if re.fullmatch(pattern, value):
                    evidence = (f"block abbreviation indicates {label.lower()}: {value}", f"layer={entity.layer}")
                    return label, Classification(label, 0.88, "hybrid_rules", evidence, _state(0.88))
    if entity.entity_type not in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
        # A tag such as "SD1" or a note "방화문 상세" on a door layer annotates a door; it is not one.
        sources.append(("layer", layer, 0.0))
    for source, value, bonus in sources:
        found = _match(value)
        if not found and source == "block":
            # Last resort for block names only: a type prefix such as "D-EL-현관" / "W_1500".
            prefix = re.match(r"([dw])[-_]", value)
            if prefix:
                found = ("Door" if prefix.group(1) == "d" else "Window", "block name prefix indicates opening", 0.8, prefix.group(0))
        if found:
            label, reason, base_confidence, token = found
            confidence = round(min(base_confidence + bonus, 0.95), 2)
            evidence = (f"{source} {reason}: {token}", f"entity_type={entity.entity_type}", f"layer={entity.layer}")
            return label, Classification(label, confidence, "hybrid_rules", evidence, _state(confidence))
    if entity.entity_type not in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "INSERT"}:
        for pattern, label, reason, confidence in LAYER_FALLBACK_RULES:
            if re.search(pattern, layer):
                evidence = (f"layer {reason}", f"entity_type={entity.entity_type}", f"layer={entity.layer}")
                return label, Classification(label, confidence, "layer_convention_rules", evidence, _state(confidence))
    if entity.entity_type in {"TEXT", "MTEXT"}:
        confidence = 0.78
        return "Annotation", Classification("Annotation", confidence, "entity_type_rule", ("entity_type=text", f"layer={entity.layer}"), _state(confidence))
    confidence = 0.35
    return "CADEntity", Classification("CADEntity", confidence, "fallback", (f"entity_type={entity.entity_type}", f"layer={entity.layer}"), _state(confidence))


# Element classes whose outline a generic hatch may fill.
_HATCH_HOSTS = ("Wall", "Slab", "Roof", "Column", "Beam", "Stair", "Foundation", "Ramp")


def _bbox_iou(a: dict[str, float], b: dict[str, float]) -> float:
    keys = ("min_x", "min_y", "max_x", "max_y")
    if not all(k in a and k in b for k in keys):
        return 0.0
    ix = max(0.0, min(a["max_x"], b["max_x"]) - max(a["min_x"], b["min_x"]))
    iy = max(0.0, min(a["max_y"], b["max_y"]) - max(a["min_y"], b["min_y"]))
    inter = ix * iy
    area = lambda r: max(0.0, r["max_x"] - r["min_x"]) * max(0.0, r["max_y"] - r["min_y"])
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def refine_with_context(objects: list[CAIRObject], entities: list[NormalizedCADEntity]) -> list[CAIRObject]:
    """Sheet-level refinement that needs neighbours: a hatch on a generic hatch layer (A-HATCH) is the fill
    of the element outline it coincides with (bbox IoU >= 0.9), so it takes that element's class.
    ``objects`` and ``entities`` are index aligned; objects are updated in place and returned."""
    hosts = [(obj, entity) for obj, entity in zip(objects, entities)
             if obj.type in _HATCH_HOSTS and entity.entity_type != "HATCH" and entity.bbox]
    for obj, entity in zip(objects, entities):
        if entity.entity_type != "HATCH" or not entity.bbox or not obj.classification \
                or obj.classification.method != "layer_convention_rules":
            continue
        matches = sorted(((_bbox_iou(entity.bbox, h.bbox), host.source.entity_id or "", host) for host, h in hosts), key=lambda m: (-m[0], m[1]))
        if matches and matches[0][0] >= 0.9:
            iou, _, host = matches[0]
            confidence = 0.8
            evidence = (f"hatch fills {host.type} outline {host.source.entity_id} (bbox IoU {iou:.2f})", *obj.classification.evidence)
            obj.type = host.type
            obj.classification = Classification(host.type, confidence, "hatch_fill_of_outline", evidence, _state(confidence))
            obj.properties["fills"] = host.id
    return objects


def _title_block(entity: NormalizedCADEntity) -> str | None:
    """Evidence string when an INSERT is a title block (도곽): title attributes, or a title-ish block name/layer."""
    props = entity.properties
    fields = title_block_fields(props.get("attributes") or {})
    if fields:
        return "attribute tags indicate title block: " + ", ".join(sorted(fields))
    names = " ".join(str(props.get(k) or "") for k in ("effective_name", "block_name") if not str(props.get(k) or "").startswith("*"))
    for source, value in (("block", names.lower()), ("layer", entity.layer.lower())):
        evidence = _Evidence(value)
        for token in TITLE_BLOCK_TOKENS:
            if value.strip() and _token_matches(evidence, token):
                return f"{source} name indicates title block: {token.lstrip('=^')}"
    return None


def semantic_class(entity: NormalizedCADEntity, label: str | None = None) -> tuple[str, dict[str, Any]]:
    """Finer evidence-based class for reporting and retrieval, plus the properties that justify it.

    The ontology type stays ``label`` (a room-name TEXT is still an Annotation object); this adds what the
    annotation denotes: a ``Space`` (room vocabulary or an area-identification layer), a ``SteelSection``
    (section designation), or the class of a tagged element (mark such as SD1).
    """
    label = label or classify(entity)[0]
    if entity.entity_type not in {"TEXT", "MTEXT", "ATTRIB"}:
        return label, {}
    text = str(entity.properties.get("text") or "").strip()
    room = room_from_text(text)
    layer = entity.layer.lower()
    if not room and re.search(r"area-?iden|room-?(?:name|iden)|실명", layer) and 0 < len(text) <= 20 \
            and re.search(r"[A-Za-z가-힣]", text):
        room = _room_name_fields(text.split("\n")[0].strip())
    if room:
        return "Space", room
    sections = steel_sections(text)
    if sections:
        return "SteelSection", {"sectionDesignation": sections[0]["sectionDesignation"],
                                "sectionDesignations": [s["sectionDesignation"] for s in sections]}
    return label, {}


# Text-derived classes that replace the Annotation type of their label (with the minimum confidence used).
PROMOTED_CLASSES: dict[str, float] = {"Space": 0.82, "SteelSection": 0.82}


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


def _room_name_fields(name: str) -> dict[str, Any]:
    """Preserve the displayed room name while adding stable indexed-name search aliases."""
    display = re.sub(r"\s+", " ", str(name)).strip()
    result: dict[str, Any] = {"roomName": display}
    indexed = re.fullmatch(r"(?P<base>.*?\D)\s*(?P<index>\d{1,2})", display)
    if not indexed:
        return result
    base = indexed["base"].strip()
    index = indexed["index"]
    # Only normalize numeric suffixes for a known room vocabulary item. This avoids
    # treating arbitrary labels such as "A101" or drawing numbers as room names.
    known = {re.sub(r"\s+", " ", value).strip().casefold() for value in ROOM_NAMES}
    if base.casefold() not in known:
        return result
    compact = f"{base}{index}"
    spaced = f"{base} {index}"
    aliases = list(dict.fromkeys((display, compact, spaced)))
    result["roomNameNormalized"] = compact
    result["roomNameAliases"] = aliases
    return result


def room_from_text(text: str) -> dict[str, Any] | None:
    """Parse '거실', '101호 회의실', '침실1 12.5㎡', 'LIVING ROOM (24.3 m2)' into room properties."""
    lines = [line.strip() for line in re.split(r"[\r\n]+|\\P", str(text)) if line.strip()]
    if not lines or len(lines) > 3 or len(lines[0]) > 40:
        return None
    found = _ROOM_RE.match(lines[0])
    if not found:
        return None
    result: dict[str, Any] = _room_name_fields(found["name"])
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
    ("일람표", "schedule", (r"일람", r"SCHEDULE", r"리스트표", r"부재표", r"마감표")),
    ("창호도", "door_window", (r"창호", r"WINDOW\s+(ELEVATION|DETAIL)", r"DOOR\s*(&|AND)?\s*WINDOW")),
    ("표지/목록", "cover_index", (r"표지", r"목록", r"COVER", r"DRAWING\s+(LIST|INDEX)", r"\bINDEX\b")),
    ("상세도", "detail", (r"상세", r"DETAIL", r"\bDET\b", r"확대")),
    ("단면도", "section", (r"단면", r"SECTION")),
    ("입면도", "elevation", (r"입면", r"정면도", r"측면도", r"배면도", r"ELEVATION")),
    ("배치도", "site_plan", (r"배치", r"SITE\s*PLAN", r"LAYOUT\s+PLAN")),
    ("평면도", "plan", (r"평면", r"\bPLAN\b", r"FLOOR", r"\d+\s*층", r"\bB?\d+F\b")),
)


# Coarse sheet groups used for discipline-level filtering (and by the classification eval).
DRAWING_CATEGORY_GROUPS = {
    "plan": "plan", "site_plan": "plan", "elevation": "elevation", "section": "section", "detail": "detail",
    "steel_detail": "detail", "door_window": "detail", "structural_plan": "structural", "schedule": "schedule",
    "cover_index": "cover", "other": "other",
}


def drawing_category(*candidates: tuple[str, str]) -> dict[str, str]:
    """First matching category over (source, text) candidates in priority order; '기타' when nothing matches."""
    for source, text in candidates:
        if not text:
            continue
        for ko, en, patterns in DRAWING_CATEGORIES:
            for pattern in patterns:
                if re.search(pattern, str(text), re.IGNORECASE):
                    return {"drawing_category": ko, "drawing_category_en": en,
                            "drawing_category_group": DRAWING_CATEGORY_GROUPS[en],
                            "drawing_category_source": source, "drawing_category_evidence": str(text)[:120]}
    return {"drawing_category": "기타", "drawing_category_en": "other", "drawing_category_group": "other",
            "drawing_category_source": "none",
            "drawing_category_evidence": ""}


# Storey tokens as Korean drawings write them: 지하1층/B1F -> B1, 3층/3F/3FL -> 3F, 지붕·옥상·옥탑 -> RF
# (same names as spatial_relations.storey_from_sheet).
# Ranges such as "1~3층" or "지하1 ~ 지하3층" are removed first, so a multi-storey sheet stays unassigned.
_STOREY_RANGE = re.compile(
    r"(?:지하|지상|(?<![A-Z0-9])B)?\s*\d{1,3}\s*(?:층|FL|F)?\s*[~\-–～]\s*(?:지하|지상|B)?\s*\d{1,3}\s*(?:층|FL|F)?",
    re.IGNORECASE,
)
_STOREY_TOKEN = re.compile(
    r"(?:지하\s*(?P<bn>\d{1,2})\s*층)"
    r"|(?:(?<![A-Z0-9])B\s*(?P<bn2>\d{1,2})\s*(?:FL|F|층)?(?![A-Z0-9]))"
    r"|(?P<roof>지붕|옥상|옥탑|(?<![A-Z0-9])(?:RF|ROOF)(?![A-Z0-9]))"
    r"|(?:(?<![A-Z0-9~\-])(?P<fn>\d{1,3})\s*(?:층|(?:FL|F)(?![A-Z0-9])))",
    re.IGNORECASE,
)


def storey_tokens(text: str) -> set[str]:
    """Distinct normalised storeys named in ``text`` ('B1', '3F', 'RF')."""
    found = set()
    for m in _STOREY_TOKEN.finditer(_STOREY_RANGE.sub(" ", str(text or ""))):
        if m.group("bn") or m.group("bn2"):
            found.add(f"B{int(m.group('bn') or m.group('bn2'))}")
        elif m.group("roof"):
            found.add("RF")
        elif m.group("fn") and int(m.group("fn")) > 0:
            found.add(f"{int(m.group('fn'))}F")
    return found


def storey_from(*candidates: tuple[str, str]) -> dict[str, str]:
    """First candidate naming exactly one storey, in priority order; {} when none or only ambiguous ones."""
    for source, text in candidates:
        tokens = storey_tokens(text) if text else set()
        if len(tokens) == 1:
            return {"storey": tokens.pop(), "storey_source": source, "storey_evidence": str(text)[:120]}
    return {}


def normalize_storey(value: str | None) -> str | None:
    """Query-side normalisation: '2층', '2f', 'B1F', '지하1층' -> '2F', 'B1'; unknown text is returned stripped."""
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    tokens = storey_tokens(text)
    if len(tokens) == 1:
        return tokens.pop()
    if m := re.fullmatch(r"B\s*(\d{1,2})", text, re.IGNORECASE):
        return f"B{int(m.group(1))}"
    if text.isdigit() and int(text) > 0:
        return f"{int(text)}F"
    return text


TITLE_BLOCK_KEYS: dict[str, tuple[str, ...]] = {
    "drawingNumber": ("DWG_NO", "DWGNO", "DRAWING_NO", "DRAWINGNO", "DRAWING_NUMBER", "DWG_NUMBER", "SHEET_NO", "SHEETNO",
                      "DNO", "도면번호", "도번", "시트번호"),
    "drawingTitle": ("TITLE", "DWG_TITLE", "DRAWING_TITLE", "DWG_NAME", "DRAWING_NAME", "SHEET_TITLE", "NAME_OF_DRAWING", "도면명", "도면제목", "제목"),
    "scale": ("SCALE", "축척", "SCALE_A1", "SCALE_A3"),
    "date": ("DATE", "일자", "날짜", "작성일"),
    "revisionLabel": ("REV", "REVISION", "REV_NO", "개정", "개정번호"),
    "projectName": ("PROJECT", "PROJECT_NAME", "PROJ_NAME", "공사명", "프로젝트", "사업명",
                    "PROJECT_TITLE", "작품명"),
    "designer": ("DESIGNER", "DESIGNED_BY", "설계", "설계자", "건축사"),
    "supervisor": ("SUPERVISOR", "감리", "감리자"),
    "client": ("CLIENT", "OWNER", "건축주"),
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
    finer, finer_props = semantic_class(entity, label)
    if finer in PROMOTED_CLASSES and finer != label:
        # A room label denotes the room and a section designation denotes the profile: the text is the
        # only evidence of them on a 2D sheet, so it becomes that object (ontology type + classification).
        base = classification.confidence if classification else 0.78
        confidence = round(min(max(base, PROMOTED_CLASSES[finer]), 0.95), 2)
        evidence = (f"text denotes {finer}: {finer_props.get('roomName') or finer_props.get('sectionDesignation')}",
                    *(classification.evidence if classification else ()))
        classification = Classification(finer, confidence, "text_semantics", evidence, _state(confidence))
        finer_props = {"annotation_role": label, **finer_props}
        label = finer
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
        properties={"cad_entity_type": entity.entity_type, "layer": entity.layer, **entity.properties,
                    **({"semantic_class": finer} if finer != label else {}), **finer_props},
        classification=classification,
        provenance=provenance,
    )
