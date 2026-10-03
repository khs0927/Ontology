"""Korean AEC question handling in the operational hybrid search.

The parsing tests need no database. The retrieval test ingests the Korean floor-plan fixtures and runs
only when AEC_TEST_DATABASE_URL points at a disposable PostgreSQL with AGE, pgvector and pg_trgm.
"""

import os
import re
import shutil
from pathlib import Path

import pytest

from aec_intelligence.operational.search import like_pattern, normalize_storey, parse_query, storey_pattern

DSN = os.getenv("AEC_TEST_DATABASE_URL")
FIXTURES = Path(__file__).parent / "fixtures" / "drawings_ko"


@pytest.mark.parametrize("query,terms,storey,kind", [
    ("1층 침실", ["침실"], "1F", None),
    ("2층 방", [], "2F", "Space"),
    ("1층 평면도 방 목록", ["평면도"], "1F", "Space"),
    ("지하1층 기계실", ["기계실"], "B1", None),
    ("B2 주차장", ["주차장"], "B2", None),
    ("3F 화장실", ["화장실"], "3F", None),
    ("H형강", ["H-", "형강"], None, None),
    ("H-400x200", ["H-400x200"], None, None),
    ("침실1", ["침실1"], None, None),
])
def test_parse_query_extracts_storey_and_room_intent(query, terms, storey, kind):
    parsed = parse_query(query)
    assert (parsed.terms, parsed.storey, parsed.kind) == (terms, storey, kind)


def test_normalize_storey_spellings():
    assert {normalize_storey(s) for s in ("1층", "1F", "1fl", " 1층 ")} == {"1F"}
    assert {normalize_storey(s) for s in ("지하1층", "B1", "B1F")} == {"B1"}
    assert normalize_storey("지붕") == "RF" and normalize_storey("") is None


def test_storey_pattern_does_not_match_other_floors():
    one = re.compile(storey_pattern("1F"), re.IGNORECASE)
    assert one.search("A-101_1층평면도.dxf") and one.search("평면도 1F") and one.search("1 층")
    assert not one.search("A-111_11층평면도.dxf") and not one.search("21F") and not one.search("지하1층")
    b1 = re.compile(storey_pattern("B1"), re.IGNORECASE)
    assert b1.search("지하1층 평면도") and b1.search("B1F") and not b1.search("B12")


def test_like_pattern_escapes_wildcards():
    assert like_pattern("100%_a") == "%100\\%\\_a%"


@pytest.fixture()
def korean_stack(tmp_path):
    if not DSN:
        pytest.skip("AEC_TEST_DATABASE_URL not set")
    pytest.importorskip("psycopg")
    pytest.importorskip("ezdxf")
    from aec_intelligence.operational.config import Settings
    from aec_intelligence.operational.db import Database
    from aec_intelligence.operational.worker import IngestionWorker

    imports = tmp_path / "imports"
    imports.mkdir()
    settings = Settings(dsn=DSN, data_root=tmp_path, import_roots=(imports,))
    db = Database(DSN)
    db.initialize()
    project = f"P-ko-{os.getpid()}-{tmp_path.name[-6:]}"
    for name in ("A-101_1층평면도.dxf", "A-102_2층평면도_cp949.dxf", "S-201_구조평면도.dxf"):
        source = imports / name
        shutil.copy(FIXTURES / name, source)
        db.enqueue({"source": str(source), "project_id": project}, dedup_key=f"{project}:{name}")
    worker = IngestionWorker(db, settings)
    while worker.run_once():
        pass
    return db, settings, project


def test_korean_room_steel_and_storey_questions(korean_stack):
    from aec_intelligence.operational.search import SearchRouter

    db, settings, project = korean_stack
    router = SearchRouter(db, settings)

    # An exact room name ranks the Space first, not the text it was derived from or vector-only noise.
    top = router.search("침실", project_id=project, top_k=5).hits[0]
    assert top.kind == "Space" and "침실" in top.label

    # The storey comes from the drawing name; "1층" must not return 2층 rooms.
    for result in (router.search("1층 침실", project_id=project, top_k=10),
                   router.search("침실", project_id=project, storey="1F", top_k=10)):
        assert result.hits
        assert all("1층" in h.citation.document_name for h in result.hits)

    # "2층 방" names no room: it lists the Spaces of the 2nd floor.
    rooms = router.search("2층 방", project_id=project, top_k=20).hits
    assert rooms and all(h.kind == "Space" and "2층" in h.citation.document_name for h in rooms)

    top = router.search("H형강", project_id=project, top_k=5).hits[0]
    assert top.kind == "SteelSection" and "H-" in top.label

    # Graph expansion returns the object's edges (it used to come back empty for every hit).
    space = router.search("거실", project_id=project, kind="Space", top_k=1).hits[0]
    assert space.relations
    assert all({"subject", "predicate", "object"} <= set(r) for r in space.relations)
    assert any(space.object_id in (r["subject"], r["object"]) for r in space.relations)
