"""CAD semantics of the operational DXF parser: block catalog, instances, rooms, sheets, details, steel, layers.

Every fixture is generated with ezdxf inside the test so the expectations sit next to the drawing they describe.
"""

import os
from collections import Counter
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from aec_intelligence.classifier import (  # noqa: E402
    RULES, _match, classify, detail_title, drawing_category, element_mark, room_from_text, steel_sections,
    title_block_fields,
)
from aec_intelligence.dxf import NormalizedCADEntity, decode_dxf_text, read_dxf  # noqa: E402
from aec_intelligence.ontology_model import CLASS_NAMES, PROPERTY_NAMES  # noqa: E402
from aec_intelligence.operational.config import Settings  # noqa: E402
from aec_intelligence.operational.parsers import parse_source  # noqa: E402


def _add_block(doc, name, attdefs=(), layer="0"):
    block = doc.blocks.new(name)
    block.add_line((0, 0), (900, 0), dxfattribs={"layer": layer})
    block.add_arc((0, 0), 900, 0, 90, dxfattribs={"layer": layer})
    for i, tag in enumerate(attdefs):
        block.add_attdef(tag, (0, -200 * (i + 1)), dxfattribs={"height": 100, "prompt": f"{tag}?"})
    return block


def build_drawing(path: Path) -> Path:
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    for name, color in (("A-WALL", 1), ("A-DOOR", 2), ("창호", 3), ("문자", 7), ("S-BEAM", 4), ("가구", 5), ("A-ANNO", 7)):
        doc.layers.add(name, color=color)
    _add_block(doc, "SD1")
    _add_block(doc, "AW-1")
    _add_block(doc, "변기")
    _add_block(doc, "창고표시")
    _add_block(doc, "ROOMTAG", ("ROOM_NAME", "ROOM_NO", "AREA"))
    _add_block(doc, "TB_A1", ("DWG_NO", "TITLE", "SCALE", "DATE", "REV"))
    _add_block(doc, "BEAMTAG", ("SIZE",))
    _add_block(doc, "UNKNOWN_THING")
    # Dynamic block: the anonymous *U representation names its source in AcDbBlockRepBTag XDATA.
    source = _add_block(doc, "DOOR_DYN")
    rep = _add_block(doc, "*U7")
    doc.appids.add("AcDbBlockRepBTag")
    rep.block_record.set_xdata("AcDbBlockRepBTag", [(1070, 1), (1005, source.block_record.dxf.handle)])

    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (10000, 0), (10000, 8000), (0, 8000)], close=True, dxfattribs={"layer": "A-WALL"})
    msp.add_blockref("SD1", (1000, 0), dxfattribs={"layer": "0"})
    msp.add_blockref("AW-1", (5000, 0), dxfattribs={"layer": "A-WALL"})  # block name beats the wall layer
    msp.add_blockref("변기", (8000, 7000), dxfattribs={"layer": "가구"})
    msp.add_blockref("창고표시", (500, 500))
    msp.add_blockref("*U7", (3000, 0), dxfattribs={"layer": "A-DOOR"})
    msp.add_blockref("UNKNOWN_THING", (9000, 100))
    msp.add_blockref("ROOMTAG", (2000, 6000)).add_auto_attribs({"ROOM_NAME": "회의실", "ROOM_NO": "101", "AREA": "25.5㎡"})
    msp.add_text("거실", height=200, dxfattribs={"layer": "A-ANNO"}).set_placement((4000, 4000))
    msp.add_text("창고", height=200, dxfattribs={"layer": "A-ANNO"}).set_placement((500, 1500))
    msp.add_mtext("침실1\\P12.5㎡", dxfattribs={"layer": "A-ANNO", "char_height": 150}).set_location((6000, 4000))
    msp.add_text("문자", height=100, dxfattribs={"layer": "문자"}).set_placement((100, 100))
    msp.add_text("SD1", height=100, dxfattribs={"layer": "A-DOOR"}).set_placement((1000, 300))
    msp.add_text("계단 상세도", height=250, dxfattribs={"layer": "A-ANNO"}).set_placement((20000, 0))
    msp.add_text("SECTION A-A", height=250, dxfattribs={"layer": "A-ANNO"}).set_placement((30000, 0))
    msp.add_text("상세도 참조", height=100, dxfattribs={"layer": "A-ANNO"}).set_placement((30000, 2000))
    # Steel: a beam line with its designation just above it, and a free-floating designation far away.
    msp.add_line((0, 12000), (6000, 12000), dxfattribs={"layer": "S-BEAM"})
    msp.add_text("H-400X200X8X13", height=100, dxfattribs={"layer": "S-BEAM"}).set_placement((3000, 12100))
    msp.add_text("H400*200*8*13", height=100, dxfattribs={"layer": "A-ANNO"}).set_placement((50000, 50000))
    msp.add_text("□-150x150x6", height=100, dxfattribs={"layer": "A-ANNO"}).set_placement((52000, 50000))
    beam_tag = msp.add_blockref("BEAMTAG", (0, 14000), dxfattribs={"layer": "S-BEAM"})
    beam_tag.add_auto_attribs({"SIZE": "H-588x300x12x20"})

    sheet = doc.layouts.new("A-501")
    sheet.add_blockref("TB_A1", (0, 0)).add_auto_attribs(
        {"DWG_NO": "A-501", "TITLE": "계단 상세도", "SCALE": "1/20", "DATE": "2026-10-01", "REV": "R2"})
    doc.layouts.new("철골상세")
    doc.saveas(path)
    return path


@pytest.fixture()
def parsed(tmp_path: Path):
    data_root = tmp_path / "data"
    source = build_drawing(tmp_path / "plan.dxf")
    settings = Settings(dsn="dummy", data_root=data_root, import_roots=(tmp_path,))
    return parse_source(source, "doc_blocks", data_root / "artifacts" / "doc_blocks" / "rev-0", settings, "1층 평면도.dxf")


def _by_type(result, kind):
    return [o for o in result["objects"] if o["type"] == kind]


def _one(result, kind, **props):
    found = [o for o in _by_type(result, kind) if all(o["properties"].get(k) == v for k, v in props.items())]
    assert len(found) == 1, (kind, props, [o["properties"] for o in _by_type(result, kind)])
    return found[0]


def _rels(result, predicate):
    return [r for r in result["relations"] if r["predicate"] == predicate]


def test_ids_are_unique_and_every_term_is_declared(parsed):
    ids = [o["id"] for o in parsed["objects"]]
    assert len(ids) == len(set(ids))
    rel_ids = [r["id"] for r in parsed["relations"]]
    assert len(rel_ids) == len(set(rel_ids))
    assert {o["type"] for o in parsed["objects"]} <= CLASS_NAMES
    assert {r["predicate"] for r in parsed["relations"]} <= PROPERTY_NAMES
    known = set(ids)
    assert all(r["subject"] in known and r["object"] in known for r in parsed["relations"])


def test_block_catalog_lists_every_definition_with_attdefs_and_counts(parsed):
    blocks = {o["properties"]["name"]: o for o in _by_type(parsed, "BlockDefinition")}
    assert {"SD1", "AW-1", "변기", "ROOMTAG", "TB_A1", "DOOR_DYN", "*U7", "UNKNOWN_THING"} <= set(blocks)
    assert not any(name.lower().startswith(("*model_space", "*paper_space")) for name in blocks)
    roomtag = blocks["ROOMTAG"]["properties"]
    assert [a["tag"] for a in roomtag["attribute_defs"]] == ["ROOM_NAME", "ROOM_NO", "AREA"]
    assert roomtag["insert_count"] == 1 and roomtag["entity_count_by_type"]["ATTDEF"] == 3
    assert blocks["SD1"]["properties"]["semantic_type"] == "Door"
    assert blocks["변기"]["properties"]["semantic_type"] == "Furniture"
    assert blocks["TB_A1"]["properties"]["semantic_type"] == "TitleBlock"
    assert blocks["TB_A1"]["properties"]["insert_count_by_layout"] == {"A-501": 1}
    anon = blocks["*U7"]["properties"]
    assert anon["is_anonymous"] and anon["effective_name"] == "DOOR_DYN" and anon["is_dynamic_representation"]
    assert blocks["DOOR_DYN"]["properties"]["effective_names"] == ["DOOR_DYN", "*U7"]
    assert blocks["DOOR_DYN"]["properties"]["effective_insert_count"] == 1
    assert blocks["SD1"]["properties"]["block_bbox"]["max_x"] == pytest.approx(900)
    root = _by_type(parsed, "Document")[0]["id"]
    contained = {r["object"] for r in _rels(parsed, "contains") if r["subject"] == root}
    assert {b["id"] for b in blocks.values()} <= contained
    assert parsed["metrics"]["blocks"] == len(blocks)


def test_inserts_become_typed_instances_of_their_definitions(parsed):
    blocks = {o["properties"]["name"]: o["id"] for o in _by_type(parsed, "BlockDefinition")}
    instance_of = {r["subject"]: r["object"] for r in _rels(parsed, "instanceOf")}
    door = _one(parsed, "Door", block_name="SD1")
    assert instance_of[door["id"]] == blocks["SD1"]
    window = _one(parsed, "Window", block_name="AW-1")
    assert window["properties"]["classification"]["evidence"][0].startswith("block")
    dynamic = _one(parsed, "Door", block_name="*U7")
    assert dynamic["properties"]["effective_name"] == "DOOR_DYN"
    assert instance_of[dynamic["id"]] == blocks["DOOR_DYN"]
    assert _one(parsed, "Furniture", block_name="변기")["id"] in instance_of
    unknown = _one(parsed, "CADEntity", block_name="UNKNOWN_THING")
    assert unknown["state"] == "OBSERVED" and instance_of[unknown["id"]] == blocks["UNKNOWN_THING"]
    # 창고 (storage room) is never a window.
    assert _one(parsed, "CADEntity", block_name="창고표시")
    tag = _one(parsed, "CADEntity", block_name="ROOMTAG")
    assert tag["properties"]["attributes"] == {"ROOM_NAME": "회의실", "ROOM_NO": "101", "AREA": "25.5㎡"}
    assert tag["properties"]["xscale"] == 1.0 and tag["properties"]["rotation"] == 0.0


def test_text_is_annotation_with_marks_and_never_an_element(parsed):
    texts = {o["properties"].get("text"): o for o in parsed["objects"] if o["properties"].get("text")}
    assert texts["문자"]["type"] == "Annotation"
    assert texts["SD1"]["type"] == "Annotation"
    assert texts["SD1"]["properties"]["mark"] == "SD1" and texts["SD1"]["properties"]["mark_kind"] == "Door"
    assert texts["침실1\n12.5㎡"]["type"] == "Annotation"


def test_room_names_become_spaces(parsed):
    spaces = {o["properties"]["roomName"]: o for o in _by_type(parsed, "Space")}
    assert {"거실", "창고", "침실1", "회의실"} <= set(spaces)
    assert spaces["침실1"]["properties"]["area"] == pytest.approx(12.5)
    assert spaces["회의실"]["properties"]["roomNumber"] == "101"
    assert spaces["회의실"]["properties"]["area"] == pytest.approx(25.5)
    assert all(s["state"] == "AI_INFERRED" for s in spaces.values())
    derived = {r["subject"]: r["object"] for r in _rels(parsed, "derivedFrom")}
    assert derived[spaces["거실"]["id"]] == spaces["거실"]["properties"]["source_annotation"]


def test_sheets_get_categories_and_title_blocks(parsed):
    views = {o["evidence"]["layout"]: o for o in _by_type(parsed, "View") if o["properties"].get("view_kind") != "detail"}
    sheet = views["A-501"]["properties"]
    assert sheet["drawing_category"] == "상세도" and sheet["drawing_category_source"] == "title_block"
    assert sheet["drawingNumber"] == "A-501" and sheet["scale"] == "1/20"
    assert views["철골상세"]["properties"]["drawing_category"] == "철골상세도"
    assert views["철골상세"]["properties"]["drawing_category_source"] == "layout_name"
    assert views["Model"]["properties"]["drawing_category"] == "평면도"
    assert views["Model"]["properties"]["drawing_category_source"] == "file_name"
    title = _one(parsed, "TitleBlock")
    assert title["properties"]["drawingTitle"] == "계단 상세도" and title["properties"]["revisionLabel"] == "R2"
    assert [(r["subject"], r["object"]) for r in _rels(parsed, "hasTitleBlock")] == [(views["A-501"]["id"], title["id"])]


def test_sheets_and_their_elements_get_a_storey_from_title_or_file_name(parsed):
    views = {o["evidence"]["layout"]: o for o in _by_type(parsed, "View") if o["properties"].get("view_kind") != "detail"}
    model = views["Model"]
    assert model["storey"] == "1F"
    assert model["properties"]["storey_source"] == "file_name"
    assert _one(parsed, "Document")["storey"] == "1F"
    walls = _by_type(parsed, "Wall")
    assert walls and all(w["storey"] == "1F" for w in walls)
    assert {s["storey"] for s in _by_type(parsed, "Space")} == {"1F"}


def test_detail_titles_create_detail_view_candidates(parsed):
    details = {o["properties"]["detail_title"]: o for o in _by_type(parsed, "View") if o["properties"].get("view_kind") == "detail"}
    assert set(details) == {"계단 상세도", "SECTION A-A"}
    assert details["SECTION A-A"]["properties"]["drawing_category"] == "단면도"
    assert details["계단 상세도"]["bbox"] and details["계단 상세도"]["state"] == "AI_INFERRED"
    note = next(o for o in parsed["objects"] if o["properties"].get("text") == "상세도 참조")
    assert "detail_title" not in note["properties"]


def test_steel_sections_are_normalized_and_linked_only_on_direct_evidence(parsed):
    sections = Counter(o["properties"]["sectionDesignation"] for o in _by_type(parsed, "SteelSection"))
    assert sections == Counter({"H-400x200x8x13": 2, "SHS-150x150x6": 1, "H-588x300x12x20": 1})
    linked = _rels(parsed, "hasSection")
    beam = _one(parsed, "Beam", **{"classification": next(o for o in _by_type(parsed, "Beam")
                                                          if o["properties"].get("cad_entity_type") is None
                                                          and "block_name" not in o["properties"])["properties"]["classification"]})
    by_id = {o["id"]: o for o in parsed["objects"]}
    targets = {by_id[r["object"]]["properties"]["sectionDesignation"]: by_id[r["subject"]] for r in linked}
    assert targets["H-400x200x8x13"]["id"] == beam["id"]
    assert targets["H-588x300x12x20"]["properties"]["block_name"] == "BEAMTAG"
    assert "SHS-150x150x6" not in targets  # floating text with no member nearby stays unlinked
    assert len(linked) == 2 and all(r["state"] == "AI_INFERRED" for r in linked)


def test_layers_are_inventoried_without_per_entity_edges(parsed):
    layers = {o["properties"]["name"]: o["properties"] for o in _by_type(parsed, "Layer")}
    assert {"A-WALL", "문자", "S-BEAM", "가구"} <= set(layers)
    assert layers["A-WALL"]["color"] == 1 and layers["A-WALL"]["entity_count"] == 2
    assert layers["A-WALL"]["semantic_type"] == "Wall" and layers["문자"]["semantic_type"] == ""
    assert not _rels(parsed, "onLayer")
    unused = {"창호"}  # declared but never drawn on
    assert not unused & set(layers)


def test_cp949_legacy_dxf_and_mislabelled_codepage_decode(tmp_path: Path):
    doc = ezdxf.new("R2000")
    doc.layers.add("LAYERPLACEHOLDER")
    doc.modelspace().add_text("TEXTPLACEHOLDER", dxfattribs={"height": 100, "layer": "LAYERPLACEHOLDER"})
    template = tmp_path / "template.dxf"
    doc.saveas(template, encoding="cp1252")
    text = template.read_bytes().decode("cp1252").replace("TEXTPLACEHOLDER", "거실").replace("LAYERPLACEHOLDER", "문자")
    proper = tmp_path / "proper.dxf"
    proper.write_bytes(text.replace("ANSI_1252", "ANSI_949").encode("cp949"))
    mislabelled = tmp_path / "mislabelled.dxf"
    mislabelled.write_bytes(text.encode("cp949"))
    for path in (proper, mislabelled):
        loaded, warnings = read_dxf(path)
        assert [(e.dxf.text, e.dxf.layer) for e in loaded.modelspace()] == [("거실", "문자")], warnings
    assert any("re-read as cp949" in w for w in read_dxf(mislabelled)[1])
    settings = Settings(dsn="dummy", data_root=tmp_path / "data", import_roots=(tmp_path,))
    result = parse_source(proper, "doc_cp949", tmp_path / "data" / "out", settings)
    assert {o["properties"]["roomName"] for o in _by_type(result, "Space")} == {"거실"}


def test_unicode_escapes_and_recover_fallback(tmp_path: Path):
    assert decode_dxf_text("\\U+AC70\\U+C2E4") == "거실"
    doc = ezdxf.new("R2000")
    doc.modelspace().add_text("ROOM", dxfattribs={"height": 100})
    path = tmp_path / "noeof.dxf"
    doc.saveas(path)
    path.write_text(path.read_text(encoding="cp1252").replace("  0\nEOF\n", ""), encoding="cp1252")
    loaded, warnings = read_dxf(path)
    assert [e.dxf.text for e in loaded.modelspace()] == ["ROOM"]
    assert any("recovered with ezdxf.recover" in w for w in warnings)


@pytest.mark.parametrize("name,label", [
    ("a-wall", "Wall"), ("a-door", "Door"), ("a-window", "Window"), ("a-cols", "Column"), ("s-beam", "Beam"),
    ("방화문", "Door"), ("출입문", "Door"), ("여닫이", "Door"), ("미닫이", "Door"), ("sd-1", "Door"), ("d12", "Door"),
    ("창", "Window"), ("창호", "Window"), ("고정창", "Window"), ("미서기", "Window"), ("미닫이창", "Window"),
    ("창문", "Window"), ("aw1", "Window"), ("w3", "Window"), ("기둥", "Column"), ("c1", "Column"),
    ("큰보", "Beam"), ("보", "Beam"), ("계단", "Stair"), ("stair", "Stair"), ("변기", "Furniture"), ("세면대", "Furniture"),
    ("욕조", "Furniture"), ("wc", "Furniture"), ("sink", "Furniture"), ("ev1", "Elevator"), ("a-elev-glaz", "Window"), ("승강기", "Elevator"),
    ("grid", "Grid"), ("슬래브", "Slab"), ("벽체", "Wall"),
])
def test_rule_vocabulary(name, label):
    assert _match(name)[0] == label


@pytest.mark.parametrize("name", ["창고", "a-창고", "문자", "주문", "정보", "보일러", "벽지", "color", "fd", "sd", "0", "defpoints", "*d12"])
def test_rule_vocabulary_avoids_false_positives(name):
    assert _match(name) is None or name == "*d12"
    entity = NormalizedCADEntity("1", "INSERT", "0", properties={"block_name": name.upper()})
    if name.startswith("*"):
        assert classify(entity)[0] == "CADEntity"


def test_rules_declare_only_known_classes():
    assert {label for label, *_ in RULES} <= CLASS_NAMES


def test_text_helpers():
    assert room_from_text("101호 회의실") == {"roomName": "회의실", "roomNumber": "101"}
    assert room_from_text("LIVING ROOM (24.3 m2)") == {"roomName": "LIVING ROOM", "area": 24.3}
    assert room_from_text("안방 18.5m2") == {"roomName": "안방", "area": 18.5}
    assert room_from_text("거실 바닥 마감") is None
    assert [s["sectionDesignation"] for s in steel_sections("L-75x75x6, C-150x75, PIPE-114.3x4.5, PL-12")] == [
        "L-75x75x6", "C-150x75", "PIPE-114.3x4.5", "PL-12"]
    assert steel_sections("H=3000 C24 CH-1 H-300") == []
    assert element_mark("AW-03") == {"mark": "AW03", "mark_kind": "Window"}
    assert element_mark("SD") is None
    assert detail_title("주의: 상세 참조") is None
    assert drawing_category(("t", "창호일람표"))["drawing_category"] == "일람표"
    assert drawing_category(("t", "구조평면도"))["drawing_category"] == "구조평면도"
    assert drawing_category(("t", "도면목록"))["drawing_category"] == "표지/목록"
    assert drawing_category(("t", "xyz"))["drawing_category"] == "기타"
    assert title_block_fields({"도면번호": "S-201", "도면명": "2층 구조평면도"}) == {"drawingNumber": "S-201", "drawingTitle": "2층 구조평면도"}
    assert title_block_fields({"TITLE": "x"}) is None


@pytest.mark.skipif(not os.getenv("AEC_TEST_DATABASE_URL"), reason="AEC_TEST_DATABASE_URL not set")
def test_generated_drawing_lands_in_postgres_with_block_edges(tmp_path: Path):
    pytest.importorskip("psycopg")
    from aec_intelligence.operational.db import Database, graph_name
    from aec_intelligence.operational.worker import IngestionWorker

    dsn = os.environ["AEC_TEST_DATABASE_URL"]
    imports = tmp_path / "imports"
    imports.mkdir()
    source = build_drawing(imports / "blocks.dxf")
    settings = Settings(dsn=dsn, data_root=tmp_path, import_roots=(imports,))
    db = Database(dsn)
    db.initialize()
    project = f"P-blocks-{os.getpid()}"
    db.enqueue({"source": str(source), "project_id": project, "document_id": f"doc_blocks_{os.getpid()}"},
               dedup_key=f"blocks:{project}")
    assert IngestionWorker(db, settings).run_once()
    with db.connect() as conn:
        job = conn.execute("SELECT state, error FROM aec.jobs WHERE dedup_key=%s", (f"blocks:{project}",)).fetchone()
        assert job["state"] == "SUCCEEDED", job["error"]
        kinds = {r["kind"] for r in conn.execute("SELECT DISTINCT kind FROM aec.objects WHERE project_id=%s", (project,))}
        edges = db.cypher(conn, graph_name(project), "MATCH ()-[r:Rel]->() WHERE r.kind = 'instanceOf' RETURN count(r)")
    assert {"BlockDefinition", "Layer", "Space", "TitleBlock", "SteelSection", "Door", "Window", "Furniture"} <= kinds
    assert int(str(edges[0]["value"])) >= 7


def test_korean_fixture_classification_eval_gate():
    """Regression gate on the golden Korean drawing set (scripts/eval_classification.py)."""
    import importlib.util
    import json

    root = Path(__file__).parents[1]
    fixtures = root / "tests" / "fixtures" / "drawings_ko"
    if not (fixtures / "labels.json").is_file():
        pytest.skip("Korean fixture set not present")
    spec = importlib.util.spec_from_file_location("eval_classification", root / "scripts" / "eval_classification.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.evaluate(json.loads((fixtures / "labels.json").read_text(encoding="utf-8")), fixtures, root / "src")
    assert report["entities"]["accuracy"] >= 0.95 and report["entities"]["macro_f1"] >= 0.95, report["confusion"]
    assert report["sheets"]["category_accuracy"] == 1.0 and report["sheets"]["number_accuracy"] == 1.0
    assert report["blocks"]["accuracy"] >= 0.95
    # No false positives: every remaining miss is an abstention, never a wrong class.
    assert all(row["predicted"] == "(unclassified)" for row in report["confusion"]), report["confusion"]
