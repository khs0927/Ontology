"""Entity resolution rules: one canonical key per project, drawing series, storey, room and steel section.

All functions are pure so the rules are unit-tested without a database. Every merge they cause is
recorded as an alias row (aec.kg_aliases) by the builder, so a wrong merge can be traced and undone.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import PurePath, PureWindowsPath

from ...classifier import normalize_storey, storey_tokens
from ..parsers import filename_sheet_fields

# Folder names that group projects rather than being one ("###프로젝트/양산가산산업단지").
CONTAINER_WORDS = ("프로젝트", "감리", "공모", "현상", "작업중", "웹하드", "회사", "projects", "project")
# A second-level folder with one of these names is a phase of the first-level project
# ("학장동 574-29/#허가", "/#사용승인", "/#실시").
PHASE_WORDS = ("허가", "사용승인", "실시", "착공", "기본", "계획", "설계", "변경", "준공", "실시설계", "기본설계",
               "계획설계", "인허가", "건축허가", "공사", "시공", "견적", "심의", "구조", "전기", "기계", "소방", "토목",
               "조경", "통신", "설비", "도면", "서류", "참고", "자료")

DISCIPLINE_PREFIX = {"A": "ARCH", "AR": "ARCH", "S": "STRUCT", "ST": "STRUCT", "M": "MECH", "ME": "MECH",
                     "P": "PLUMB", "E": "ELEC", "EL": "ELEC", "F": "FIRE", "FP": "FIRE", "C": "CIVIL",
                     "L": "LAND", "T": "COMM", "I": "INTERIOR", "G": "GENERAL"}
DISCIPLINE_WORDS = (("구조", "STRUCT"), ("철골", "STRUCT"), ("전기", "ELEC"), ("통신", "COMM"), ("기계", "MECH"),
                    ("설비", "MECH"), ("위생", "PLUMB"), ("소방", "FIRE"), ("토목", "CIVIL"), ("조경", "LAND"),
                    ("인테리어", "INTERIOR"), ("건축", "ARCH"))
DISCIPLINE_KO = {"ARCH": "건축", "STRUCT": "구조", "MECH": "기계", "PLUMB": "위생", "ELEC": "전기", "FIRE": "소방",
                 "CIVIL": "토목", "LAND": "조경", "COMM": "통신", "INTERIOR": "인테리어", "GENERAL": "일반"}

# Korean words for the element kinds the parser emits, used in node names and query linking.
KIND_KO = {"Wall": "벽", "Column": "기둥", "Beam": "보", "Door": "문", "Window": "창호", "Stair": "계단",
           "Elevator": "엘리베이터", "Furniture": "가구", "Bolt": "볼트", "Grid": "그리드", "Space": "실",
           "SteelSection": "철골 단면", "BuildingElementProxy": "기타 부재", "Slab": "슬래브", "Roof": "지붕"}
KIND_QUERY_WORDS = {
    "Door": ("문", "도어", "출입문", "방화문", "door"), "Window": ("창호", "창문", "창", "window"),
    "Column": ("기둥", "column"), "Beam": ("보", "beam", "거더"), "Wall": ("벽", "벽체", "wall"),
    "Stair": ("계단", "stair"), "Elevator": ("엘리베이터", "승강기", "elevator"), "Furniture": ("가구",),
    "Bolt": ("볼트", "bolt"), "Grid": ("그리드", "통심", "grid"),
}
ELEMENT_KINDS = tuple(KIND_QUERY_WORDS) + ("BuildingElementProxy",)

_SPACE_RE = re.compile(r"\s+")
_DATE_RE = re.compile(
    r"(?:(?<=[_\-\s(\[])|^)(?P<d>(?:20)?\d{2}[01]\d[0-3]\d|[01]\d[0-3]\d)(?=$|[_\-\s)\].])")
_COPY_RE = re.compile(
    r"(\s*-\s*복사본(\s*\(\d+\))?|복사본|\s*\(\d{1,2}\)$|\bcopy\b|_?최종본?|_?수정본?|_?final|"
    r"[_\s-]?rev\.?\s*\d+|[_\s-]r\d{1,2}$|[_\s-]v\d{1,2}$|\(수정\)|\(최종\))", re.IGNORECASE)


def slug(value: str) -> str:
    """Stable, readable key text: NFC, no '#', single dashes, case-folded latin."""
    text = unicodedata.normalize("NFC", str(value or "")).strip().strip("#").strip()
    text = re.sub(r"[\s,_/\\]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text.casefold() or "unknown"


def short_hash(*parts: str, n: int = 16) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:n]


def _clean_folder(name: str) -> str:
    return unicodedata.normalize("NFC", name).strip().lstrip("#").strip()


def _is_container(name: str) -> bool:
    low = _clean_folder(name).casefold()
    return low in CONTAINER_WORDS or (name.startswith("###") and len(_clean_folder(name)) <= 6)


def _is_phase(name: str) -> bool:
    clean = _clean_folder(name)
    return any(clean == word or clean.startswith(word) and len(clean) <= len(word) + 3 for word in PHASE_WORDS)


def _parts(top_folder: str) -> list[str]:
    return [p for p in re.split(r"[\\/]+", str(top_folder)) if p.strip()]


def container_folders(top_folders) -> set[str]:
    """First-level folders that group several projects: two or more second-level children that are not
    phase folders ("계획/화목동698-14", "계획/..."; "용변/남천동", "용변/주례동"). A folder whose children are
    only phases ("학장동 574-29/#허가", "/#사용승인") stays one project."""
    children: dict[str, set[str]] = {}
    for top in top_folders:
        if not top:
            continue
        parts = _parts(top)
        if len(parts) >= 2 and not _is_phase(parts[1]):
            children.setdefault(parts[0], set()).add(parts[1])
    return {first for first, kids in children.items() if len(kids) >= 2}


def canonical_project(project_id: str, top_folder: str | None = None,
                      containers: set[str] | frozenset[str] = frozenset()) -> dict[str, str]:
    """Canonical project for a document: ``{"key", "name", "phase"}``.

    ``top_folder`` is the census' first two folder levels under an import root. Without it (manual
    imports, eval sets) the stored ``project_id`` is the project. ``containers`` comes from
    ``container_folders`` over the whole corpus.
    """
    if top_folder:
        parts = _parts(top_folder)
        phase = ""
        if len(parts) >= 2 and (_is_container(parts[0]) or parts[0] in containers):
            name = parts[1]
            if len(parts) >= 3 and _is_phase(parts[2]):
                phase = _clean_folder(parts[2])
        elif len(parts) >= 2 and _is_phase(parts[1]):
            name, phase = parts[0], _clean_folder(parts[1])
        else:
            name = parts[0] if parts else project_id
        name = _clean_folder(name) or project_id
        return {"key": slug(name), "name": name, "phase": phase}
    name = re.sub(r"^P-", "", str(project_id or "unknown"))
    return {"key": slug(name), "name": name, "phase": ""}


def date_token(name: str) -> str | None:
    """The revision date written into a file name (``_0611``, ``(20240626)``, ``-240701``) as YYMMDD/MMDD."""
    stem = re.sub(r"\.(dwg|dxf|pdf)$", "", PurePath(str(name)).name, flags=re.IGNORECASE)
    found = None
    for match in _DATE_RE.finditer(stem):
        token = match["d"]
        mm, dd = int(token[-4:-2]), int(token[-2:])
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            found = token[-6:] if len(token) >= 6 else token
    return found


def series_key(name: str, sheet_number: str | None = None) -> str:
    """Key shared by every revision/copy of one drawing: its sheet number, else its title without the
    date token and copy markers (``배치도_0611.dwg`` and ``배치도_0626 - 복사본.dwg`` -> ``배치도``)."""
    if sheet_number:
        return "no:" + sheet_number.upper().replace(" ", "")
    stem = re.sub(r"\.(dwg|dxf|pdf)$", "", PurePath(str(name)).name, flags=re.IGNORECASE)
    stem = _DATE_RE.sub("", stem)
    for _ in range(3):
        stem = _COPY_RE.sub("", stem)
    stem = re.sub(r"[\s_\-()\[\].]+", "", stem)
    return "t:" + unicodedata.normalize("NFC", stem).casefold()


def drawing_fields(name: str, path: str | None = None) -> dict[str, str | None]:
    """Sheet number, title, revision date, discipline and storeys read from a drawing's file name/path."""
    number, title = filename_sheet_fields(name)
    disc = None
    if number:
        prefix = re.match(r"[A-Z]{1,2}", number.upper())
        disc = DISCIPLINE_PREFIX.get(prefix.group(0)) if prefix else None
        if disc is None and prefix:
            disc = DISCIPLINE_PREFIX.get(prefix.group(0)[0])
    if disc is None:
        haystack = f"{PureWindowsPath(str(path)).parent if path else ''} {name}"
        disc = next((code for word, code in DISCIPLINE_WORDS if word in haystack), None)
    storeys = sorted(storey_tokens(f"{title or ''} {name}"))
    return {"sheet_number": number, "title": title, "date": date_token(name), "discipline": disc,
            "storeys": storeys, "series": series_key(name, number)}


def canonical_storey(value: str | None) -> str | None:
    return normalize_storey(value) if value else None


def storey_sort_key(level: str) -> tuple[int, int]:
    if level.startswith("B") and level[1:].isdigit():
        return (0, -int(level[1:]))
    if level.endswith("F") and level[:-1].isdigit():
        return (1, int(level[:-1]))
    if level == "RF":
        return (2, 0)
    return (3, 0)


def canonical_room(name: str | None) -> str | None:
    """Room merge key: NFC, no inner spaces, case-folded (``회의실 1`` = ``회의실1``; ``PS`` = ``ps``)."""
    if not name:
        return None
    text = unicodedata.normalize("NFC", str(name))
    text = _SPACE_RE.sub("", text).strip(" .:-_").casefold()
    return text or None


def canonical_section(designation: str | None) -> str | None:
    """``H-300X150X6.5X9`` / ``H300*150*6.5*9`` / ``H 300x150x6.5x9`` -> ``H-300x150x6.5x9``."""
    if not designation:
        return None
    text = str(designation).upper().replace("×", "X").replace("*", "X").replace(" ", "")
    match = re.match(r"^(BH|SHS|RHS|PIPE|FB|PL|H|I|C|L|T)-?(\d.*)$", text)
    if not match:
        return text
    dims = [d for d in match.group(2).split("X") if d]
    norm = []
    for d in dims:
        try:
            value = float(d)
        except ValueError:
            return text
        norm.append(f"{value:g}")
    return f"{match.group(1)}-{'x'.join(norm)}"


def catalog_spec_key(spec: str) -> str | None:
    """hs-steel catalog spec (``H300x150x6.5x9``) to the same canonical form as drawing designations."""
    return canonical_section(spec)
