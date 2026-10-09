"""Synthetic tests for the Google Drive CAD pack builder / loader rules (no Drive data, no database)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

PACK = Path(__file__).resolve().parents[1] / "library" / "gdrive_cad"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, PACK / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


b = _load("build_gdrive_cad_pack")
loader = _load("load_gdrive_cad")


def row(path, ext="dwg", size=100, md5=None, dup_of=None, project=None):
    r = {"path": path, "ext": ext, "size": size, "md5": md5 or f"m{abs(hash(path)) % 10**9}",
         "mod": "2026-01-02T00:00:00.000Z", "project": project or "P/Q/R"}
    if dup_of:
        r["dup_of"] = dup_of
    return r


def docrow(path, ext="pdf", size=100, md5=None, dup_of=None, project=None):
    r = {"path": path, "ext": ext, "size": size, "md5": md5 or f"d{abs(hash(path)) % 10**9}",
         "mod": "2026-01-02T00:00:00.000Z", "group": "related"}
    if dup_of:
        r["dup_of"] = dup_of
    if project is not None:
        r["project"] = project
    return r


# ---- discipline rule -------------------------------------------------------------------------------
@pytest.mark.parametrize("number, code", [("A-101", "ARCH"), ("S-201", "STRUCT"), ("M-301", "MECH"),
                                          ("E-02", "ELEC"), ("P-100", "PLUMB"), ("F-001", "FIRE"),
                                          ("C-010", "CIVIL"), ("L-101", "LAND"), ("T-18", "COMM"),
                                          ("I-100", "INTERIOR"), ("MC-008", "MECH"), ("ST-101", "STRUCT")])
def test_sheet_prefix_maps_discipline(number, code):
    assert b.discipline_from_sheet(number) == code


@pytest.mark.parametrize("number", ["SH602", "RG127", "KS2019", "X-100", None, ""])
def test_unreliable_prefixes_are_not_disciplines(number):
    assert b.discipline_from_sheet(number) is None


def test_prefix_beats_keyword_and_votes():
    d = b.classify_discipline("건축/구조/A-101 평면도.dwg", "A-101", "평면도", {"structural": 900})
    assert (d["code"], d["confidence"], d["source"], d["review"]) == ("ARCH", 0.85, "sheet_prefix", False)


def test_keyword_beats_votes_and_checks_title_then_folders():
    d = b.classify_discipline("##작업중/철골샵도면/일반/상세 1.dwg", None, None, {"architecture": 900})
    assert (d["code"], d["confidence"], d["source"]) == ("STRUCT", 0.75, "keyword")
    assert not d["review"]
    # a title keyword is preferred over a generic folder
    d = b.classify_discipline("건축공사/소방설비 평면.dwg", None, "소방설비 평면", None)
    assert d["code"] == "FIRE"


def test_arch_keyword_is_lowest_priority_keyword():
    code, _ = b.discipline_from_keywords("프로젝트/건축구조/x.dwg", None)
    assert code == "STRUCT"


def test_layer_votes_capped_and_reviewed():
    d = b.classify_discipline("misc/foo.dwg", None, None, {"architecture": 1000, "structural": 1})
    assert d["source"] == "layer_votes" and d["code"] == "ARCH"
    assert d["confidence"] <= 0.8
    assert d["review"] is False
    mixed = b.classify_discipline("misc/foo.dwg", None, None, {"architecture": 50, "electrical": 50})
    assert mixed["confidence"] == 0.65 and mixed["review"] is True


def test_too_few_votes_fall_back_to_general_review():
    d = b.classify_discipline("misc/foo.dwg", None, None, {"architecture": 3})
    assert (d["code"], d["confidence"], d["source"], d["review"]) == ("GENERAL", 0.4, "default", True)
    assert b.classify_discipline("misc/foo.dwg", None, None, None)["review"] is True


# ---- sheet identity --------------------------------------------------------------------------------
def test_temp_dxf_sheet_names_are_not_trusted():
    rec = {"status": "ok", "sheet": {"number": None, "title": "f000", "source": "file_name"}}
    number, title, src = b.sheet_fields("p/A-101 1층평면도.dwg", "dwg", rec)
    assert (number, src) == ("A-101", "filename")
    assert title == "1층평면도"


def test_title_block_sheet_is_trusted():
    rec = {"status": "ok", "sheet": {"number": "S-201", "title": "기초평면도", "source": "title_block"}}
    assert b.sheet_fields("p/f000.dwg", "dwg", rec) == ("S-201", "기초평면도", "title_block")


def test_filename_sheet_parser_agrees_with_local_copy():
    for name in ["A-101 1층평면도.dwg", "03_C-010 현황.dwg", "M-357 - [ 지하1층 ].dwg", "지하1층평면도.dwg",
                 "2024 설계서.dwg", "S-101~132 구조.dwg"]:
        assert b.filename_sheet_fields(name) == b._local_filename_sheet_fields(name)


# ---- dedup -----------------------------------------------------------------------------------------
def test_duplicates_collapse_to_one_node_with_copies():
    rows = [row("a/x.dwg", md5="h1", size=5), row("b/x copy.dwg", md5="h1", size=5, dup_of="a/x.dwg"),
            row("c/y.dwg", md5="h2", size=5)]
    g = b.build_graph(rows, {})
    drawings = [n for n in g["nodes"].values() if n["type"] == "Drawing"]
    assert len(drawings) == 2
    x = next(n for n in drawings if n["props"]["md5"] == "h1")
    assert x["props"]["path"] == "a/x.dwg"
    assert x["props"]["duplicate_copies"] == ["b/x copy.dwg"] and x["props"]["duplicate_count"] == 1


def test_first_copy_excluded_promotes_remaining_member():
    rows = [row("b/x.dwg", md5="h1", size=5, dup_of="excluded/AEC-INTELLIGENCE/x.dwg"),
            row("c/x.dwg", md5="h1", size=5, dup_of="excluded/AEC-INTELLIGENCE/x.dwg")]
    reps = b.group_duplicates(rows)
    assert len(reps) == 1 and reps[0]["path"] == "b/x.dwg" and reps[0]["_copies"] == ["c/x.dwg"]


# ---- parse status ----------------------------------------------------------------------------------
def test_parse_status_by_format_and_record():
    rows = [row("a/m.rvt", ext="rvt", md5="r"), row("a/n.skp", ext="skp", md5="s"), row("a/A-1 x.dwg", md5="d1"),
            row("a/B-1 y.dwg", md5="d2"), row("a/C-1 z.dwg", md5="d3")]
    recs = {"a/A-1 x.dwg": {"path": "a/A-1 x.dwg", "status": "ok", "layers": [], "counts": {}, "texts": [],
                           "rooms": {}, "blocks": [], "sheet": {"source": "file_name"}},
            "a/B-1 y.dwg": {"path": "a/B-1 y.dwg", "status": "failed", "error": "x"}}
    g = b.build_graph(rows, recs)
    st = {n["props"]["path"]: n["props"]["parse_status"] for n in g["nodes"].values() if n["type"] == "Drawing"}
    assert st["a/m.rvt"] == st["a/n.skp"] == "unparsed_format"
    assert st["a/A-1 x.dwg"] == "parsed"
    assert st["a/B-1 y.dwg"] == "parse_failed"
    assert st["a/C-1 z.dwg"] == "metadata_only"


# ---- graph contents --------------------------------------------------------------------------------
def _rec(path, layers, blocks=(), texts=(), rooms=None):
    return {"path": path, "status": "ok", "units": "millimeters", "dxf_version": "AC1032",
            "counts": {"layer_count": len(layers), "entity_count": 10}, "block_defs": len(blocks),
            "sheet": {"number": None, "title": "f000", "source": "file_name"},
            "layers": [{"name": n, "discipline": d, "element_class": "wall", "confidence": 0.9, "review": False,
                        "entities": e} for n, d, e in layers],
            "blocks": [{"name": n, "inserts": i, "category": None, "attrs": []} for n, i in blocks],
            "texts": list(texts), "rooms": rooms or {}, "text_total": len(texts),
            "discipline_votes": {"architecture": 10}}


def test_project_registration_and_edges():
    rows = [row("S/T/U/A-101 plan.dwg", md5="d1", project="S/T/U")]
    recs = {"S/T/U/A-101 plan.dwg": _rec("S/T/U/A-101 plan.dwg", [("A-WALL", "architecture", 40)],
                                         [("DOOR1", 3)], ["거실", "홍길동@example.com 010-1234-5678"],
                                         {"거실": 1})}
    g = b.build_graph(rows, recs)
    proj = g["nodes"][b.PROJECT_NODE]
    assert proj["type"] == "Project" and proj["props"]["pack"] is True and proj["props"]["drawings"] == 1
    assert sum(1 for n in g["nodes"].values() if n["type"] == "Project") == 1
    assert {a["alias_type"] for a in g["aliases"]} >= {"project_id", "project_name"}
    preds = {e["predicate"] for e in g["edges"]}
    assert preds == {"hasSubProject", "hasDrawing", "usedIn"}
    assert any(e["predicate"] == "usedIn" and g["nodes"][e["src"]]["type"] == "LayerStandard" for e in g["edges"])
    d = next(n for n in g["nodes"].values() if n["type"] == "Drawing")
    assert "거실" in d["search_text"]
    assert "@" not in d["search_text"] and "010-1234-5678" not in d["search_text"]
    assert "f000" not in d["search_text"]


def test_caps_on_global_layer_and_block_nodes():
    rows = [row(f"P/Q/R/A-{i:03d}.dwg", md5=f"d{i}") for i in range(1, 6)]
    recs = {r["path"]: _rec(r["path"], [(f"L{j}", "architecture", 5) for j in range(10)],
                            [(f"B{j}", 1) for j in range(10)]) for r in rows}
    g = b.build_graph(rows, recs, max_layers=4, max_blocks=3)
    types = [n["type"] for n in g["nodes"].values()]
    assert types.count("LayerStandard") == 4 and types.count("BlockSpec") == 3
    assert g["layers_total"] == 10 and g["blocks_total"] == 10


def test_search_text_is_bounded():
    long_texts = [("문자%d " % i) * 30 for i in range(200)]
    rows = [row("P/Q/R/A-100.dwg", md5="d1")]
    recs = {rows[0]["path"]: _rec(rows[0]["path"], [(f"LAYER{j}", "architecture", 5) for j in range(500)],
                                  texts=long_texts, rooms={f"방{j}": 1 for j in range(25)})}
    g = b.build_graph(rows, recs)
    assert all(len(n["search_text"]) <= b.MAX_SEARCH for n in g["nodes"].values())


def test_review_queue_matches_flags():
    rows = [row("P/Q/R/A-100.dwg", md5="d1"), row("P/Q/R/unknown thing.dwg", md5="d2")]
    g = b.build_graph(rows, {})
    flagged = {n["id"] for n in g["nodes"].values() if n["type"] == "Drawing" and n["props"]["review"]}
    assert {r["node_id"] for r in g["review"]} == flagged and len(flagged) == 1


def test_build_is_deterministic_and_idempotent():
    rows = [row(f"P/Q/R/S-{i:03d}.dwg", md5=f"d{i}") for i in range(1, 8)]
    recs = {rows[0]["path"]: _rec(rows[0]["path"], [("S-COL", "structural", 3)], [("B", 1)])}
    g1, g2 = b.build_graph(rows, recs), b.build_graph(list(reversed(rows)), recs)
    assert list(g1["nodes"]) == list(g2["nodes"])
    assert [json.dumps(e, sort_keys=True) for e in g1["edges"]] == [json.dumps(e, sort_keys=True) for e in g2["edges"]]


def test_load_records_last_wins_and_tolerates_partial_line(tmp_path):
    p = tmp_path / "records.jsonl"
    p.write_text('{"path":"a","status":"failed"}\n{"path":"a","status":"ok"}\n{"path":"b","sta', encoding="utf-8")
    recs, bad = b.load_records(p)
    assert recs["a"]["status"] == "ok" and bad == 1


def test_sql_is_idempotent_and_registers_pack():
    rows = [row("P/Q/R/A-100.dwg", md5="d1")]
    g = b.build_graph(rows, {})
    sql = b.render_sql(g["nodes"], g["edges"], g["aliases"])
    assert sql.startswith("-- Google Drive CAD") and sql.rstrip().endswith("COMMIT;")
    assert "DELETE FROM aec.kg_nodes WHERE project_key = 'GDRIVE_CAD'" in sql
    assert "'kg:p:GDRIVE_CAD'" in sql and '"pack": true' in sql
    assert "'gdrive-cad-pack-v1'" in sql and "-pack-" in b.FINGERPRINT
    assert sql.index("INSERT INTO aec.kg_nodes") < sql.index("UPDATE aec.kg_nodes n SET object_ids")


# ---- loader ----------------------------------------------------------------------------------------
def _vec(i):
    return {"node_id": f"GDRIVE_CAD:x:{i}", "kind": "drawing", "label": f"L{i}", "text": f"t{i}",
            "content_hash": f"h{i}"}


class FakeCursor:
    def __init__(self, log, have=()):
        self.log, self.have, self._rows = log, set(have), []

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))
        if "FROM aec.text_vectors" in sql and "ANY" in sql:
            self._rows = [(h,) for h in params[1] if h in self.have]

    def fetchall(self):
        return self._rows


def test_validate_vectors_rejects_empty_and_duplicates():
    with pytest.raises(ValueError):
        loader.validate_vectors([])
    with pytest.raises(ValueError):
        loader.validate_vectors([_vec(1), _vec(1)])
    with pytest.raises(ValueError):
        loader.validate_vectors([{"node_id": "x", "kind": "drawing", "text": "t"}])


def test_prune_is_scoped_to_project_and_refuses_empty():
    log = []
    cur = FakeCursor(log)
    loader.prune_stale_objects(cur, [_vec(1), _vec(2)])
    assert len(log) == 2 and all(p[0] == "GDRIVE_CAD" for _, p in log)
    assert "DELETE FROM aec.embeddings" in log[0][0] and "DELETE FROM aec.objects" in log[1][0]
    with pytest.raises(ValueError):
        loader.prune_stale_objects(FakeCursor([]), [])


def test_upsert_batch_embeds_only_missing_and_upserts_mappings(monkeypatch):
    calls = []
    monkeypatch.setattr(loader, "embed_many", lambda texts: calls.append(list(texts)) or [[0.0] * 1024 for _ in texts])
    log = []
    cur = FakeCursor(log, have={"h1"})
    n_new, n_obj = loader.upsert_batch(cur, [_vec(1), _vec(2)], {"drawing": "GDRIVE_CAD-DRAWING-DOC"}, set())
    assert (n_new, n_obj) == (1, 2) and calls == [["t2"]]
    sqls = [s for s, _ in log]
    assert sum("INSERT INTO aec.text_vectors" in s for s in sqls) == 1
    maps = [s for s in sqls if "INSERT INTO aec.embeddings" in s]
    assert len(maps) == 2 and all("DO UPDATE SET revision=EXCLUDED.revision" in s for s in maps)
    # the vector row precedes the mapping that references it (FK)
    assert sqls.index(next(s for s in sqls if "INSERT INTO aec.text_vectors" in s)) < sqls.index(maps[0])


def test_link_sql_is_scoped_to_project():
    assert "n.project_key = 'GDRIVE_CAD'" in loader.LINK_SQL and "o.id = n.id" in loader.LINK_SQL


def test_resolve_dsn_never_guesses(monkeypatch, tmp_path):
    for k in ("AEC_DSN", "AEC_DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    assert loader.resolve_dsn("", str(tmp_path / "missing.env")) == ""
    env = tmp_path / ".env"
    env.write_text("AEC_DATABASE_URL='postgresql://u@127.0.0.1:55432/aec'\n", encoding="utf-8")
    assert loader.resolve_dsn("", str(env)).startswith("postgresql://")
    assert loader.resolve_dsn("postgresql://x", None) == "postgresql://x"


# ---- renderer / pack identity props (synthetic names only) ----------------------------------------------
class _NoRows:
    def execute(self, *a, **k):
        return self

    def fetchall(self):
        return []


def _drawing_ctx(props):
    from aec_intelligence.operational.graphrag.ask import GraphRAG

    rag = object.__new__(GraphRAG)
    r = {"id": "n1", "type": "Drawing", "name": "SAMPLE-01 plan", "props": props, "document_ids": [], "object_ids": []}
    return rag._drawing_item(_NoRows(), r, 1.0)


def test_pack_drawing_keeps_stem_as_title_and_emits_drive_path():
    g = b.build_graph([row("Root/Proj/Sub/배관 평면도_v2.dwg", project="Root/Proj")], {})
    drw = next(n for n in g["nodes"].values() if n["type"] == "Drawing")
    assert drw["props"]["title"] == "배관 평면도_v2"
    assert drw["props"]["drive_path"] == "Root/Proj/Sub/배관 평면도_v2.dwg"
    assert drw["props"]["sub_project"] == "Root/Proj"
    assert "제목" not in drw["search_text"]  # embedded text unchanged: stem is not an extra title
    vec = next(v for v in g["vectors"] if v["node_id"] == drw["id"])
    assert vec["properties"]["drive_path"] == drw["props"]["drive_path"]


def test_renderer_shows_sub_project_and_path_only_when_pack_provides_them():
    base = {"sheet_number": "X-1", "title": "Sample title", "discipline": "FIRE"}
    plain = _drawing_ctx(base)
    assert "경로" not in plain.text and plain.path is None
    rich = _drawing_ctx({**base, "sub_project": "Root/ProjA", "drive_path": "Root/ProjA/x/X-1 Sample.dwg"})
    assert "도면번호 X-1, 제목 Sample title" in rich.text
    assert "프로젝트 ProjA" in rich.text and "경로 Root/ProjA/x/X-1 Sample.dwg" in rich.text
    assert rich.path == "Root/ProjA/x/X-1 Sample.dwg"
    assert rich.text.startswith(plain.text)  # other projects' text is a strict prefix: unchanged


# ---- documents (PDF / xls / xlsx) ------------------------------------------------------------------
def test_documents_are_deduped_metadata_nodes():
    docs = [docrow("Root/Proj/Sub/A-101 평면도.pdf", md5="h1", size=5, project="Root/Proj"),
            docrow("Root/Proj/Other/A-101 평면도 copy.pdf", md5="h1", size=5, project="Root/Proj",
                   dup_of="Root/Proj/Sub/A-101 평면도.pdf")]
    g = b.build_graph([], {}, docs=docs)
    ds = [n for n in g["nodes"].values() if n["type"] == "Document"]
    assert len(ds) == 1
    d = ds[0]
    assert d["props"]["ext"] == "pdf" and d["props"]["title"] == "A-101 평면도"
    assert d["props"]["drive_path"] == "Root/Proj/Sub/A-101 평면도.pdf"
    assert d["props"]["sub_project"] == "Root/Proj"
    assert d["props"]["duplicate_copies"] == ["Root/Proj/Other/A-101 평면도 copy.pdf"]
    assert g["documents"] == 1 and g["document_dups"] == 1
    assert any(e["predicate"] == "hasDocument" and e["src"] == b.PROJECT_NODE for e in g["edges"])
    assert {v["kind"] for v in g["vectors"]} == {"document"}


def test_document_links_to_drawing_by_stem_only_in_same_sub_project():
    rows = [row("Root/Proj/Sub/A-101 평면도.dwg", md5="d1", project="Root/Proj")]
    docs = [docrow("Root/Proj/Sub/A-101 평면도.pdf", md5="p1", project="Root/Proj"),
            docrow("Root/Proj/Sub/A-101 평면도.xlsx", ext="xlsx", md5="p2", project="Root/Proj"),
            docrow("Root/Proj/Sub/낯선이름.pdf", md5="p3", project="Root/Proj"),
            docrow("Root/Other/A-101 평면도.pdf", md5="p4", project="Root/Other")]
    g = b.build_graph(rows, {}, docs=docs)
    did = next(n["id"] for n in g["nodes"].values() if n["type"] == "Drawing")
    linked = {e["src"] for e in g["edges"] if e["predicate"] == "linkedTo" and e["dst"] == did}
    matched = [n for n in g["nodes"].values() if n["type"] == "Document" and n["id"] in linked]
    assert len(matched) == 2
    assert all(n["props"]["sub_project"] == "Root/Proj" for n in matched)
    assert g["document_links"] == 2


def test_document_links_by_sheet_number_or_title_ignoring_separators():
    rows = [row("Root/Proj/Sub/A-101 1층평면도.dwg", md5="d1", project="Root/Proj")]
    docs = [docrow("Root/Proj/Sub/A101.pdf", md5="p1", project="Root/Proj"),
            docrow("Root/Proj/Sub/1층평면도.pdf", md5="p2", project="Root/Proj")]
    g = b.build_graph(rows, {}, docs=docs)
    assert g["document_links"] == 2


def test_document_text_is_scrubbed_and_bounded():
    docs = [docrow("Root/Proj/Sub/연락 hong@example.com 010-1234-5678.pdf", md5="p1", project="Root/Proj")]
    g = b.build_graph([], {}, docs=docs)
    d = next(n for n in g["nodes"].values() if n["type"] == "Document")
    assert "@" not in d["search_text"] and "010-1234-5678" not in d["search_text"]
    assert len(d["search_text"]) <= b.MAX_SEARCH


def test_project_of_path_matches_cad_sub_project_grouping():
    assert b.project_of_path("A/B/C/D/x.dwg") == "A/B/C"
    assert b.project_of_path("1.회사/2.공모/X/Y/z.pdf") == "1.회사/2.공모/X/Y"
    assert b.project_of_path("x.pdf") == "(root)"
    assert b.project_of_path("A/B/x.pdf") == "A/B"


def test_load_documents_filters_extension_and_repo_copies(tmp_path):
    inv = [docrow("Root/Proj/Sub/a.pdf", md5="h1"),
           docrow("Root/Proj/Sub/b.xlsx", ext="xlsx", md5="h2"),
           row("Root/Proj/Sub/c.dwg", md5="h3"),
           docrow("AEC-INTELLIGENCE/copy.pdf", md5="h4"),
           docrow("revit-mcp-guideline/copy.xls", ext="xls", md5="h5")]
    p = tmp_path / "inventory.json"
    p.write_text(json.dumps(inv, ensure_ascii=False), encoding="utf-8")
    docs = b.load_documents(p)
    assert [d["path"] for d in docs] == ["Root/Proj/Sub/a.pdf", "Root/Proj/Sub/b.xlsx"]
    assert all(d["project"] == "Root/Proj/Sub" for d in docs)


def test_retrieval_fixes_sheet_aliases_and_korean_prefix():
    # 2a & 2b: Korean discipline prefix parsed as sheet_number and kg_aliases created
    rows = [row("Root/Proj/소방/소방-05 A동지상2층소방설비평면도.dwg", md5="f1", project="Root/Proj")]
    g = b.build_graph(rows, {})
    drw = next(n for n in g["nodes"].values() if n["type"] == "Drawing")
    assert drw["props"]["sheet_number"] == "소방-05"
    assert drw["props"]["title"] == "A동지상2층소방설비평면도"
    assert drw["props"]["discipline"] == "FIRE"

    aliases = [a for a in g["aliases"] if a["node_id"] == drw["id"]]
    sheet_aliases = {a["alias"] for a in aliases if a["alias_type"] == "sheet_number"}
    assert "소방-05" in sheet_aliases
    assert "소방05" in sheet_aliases


def test_retrieval_fixes_project_of_path_generic_dirs():
    # 2c: project_of_path walks up past generic directories (views, dwg, export, 도면, cad)
    assert b.project_of_path("Root/Hillside/dwg/views/2층평면도.dwg") == "Root/Hillside"
    assert b.project_of_path("Root/Proj/export/cad/도면/배치도.dwg") == "Root/Proj"


def test_retrieval_fixes_duplicate_name_siblings():
    # 2d: duplicate-name siblings include parent folder in title and search_text
    rows = [
        row("Root/1공장/도면.dwg", md5="m1", project="Root"),
        row("Root/2공장/도면.dwg", md5="m2", project="Root"),
    ]
    g = b.build_graph(rows, {})
    drawings = [n for n in g["nodes"].values() if n["type"] == "Drawing"]
    assert len(drawings) == 2
    titles = {d["props"]["title"] for d in drawings}
    assert "도면 (1공장)" in titles
    assert "도면 (2공장)" in titles
    for d in drawings:
        assert "상위폴더 1공장" in d["search_text"] or "상위폴더 2공장" in d["search_text"]


def test_retrieval_fixes_drawing_list_sheets_capped():
    # 2e: drawing list sheet text is capped to 200 chars in search_text
    long_table_text = "일련번호 " * 100
    meta = {"status": "ok", "texts": [long_table_text]}
    rows = [row("Root/Proj/C-000 도면 목록표.dwg", md5="l1", project="Root/Proj")]
    g = b.build_graph(rows, {"Root/Proj/C-000 도면 목록표.dwg": meta})
    drw = next(n for n in g["nodes"].values() if n["type"] == "Drawing")
    # search_text should not include the full 500-char table text
    assert len(drw["search_text"]) < 400



# ======================================================================================================
# PR #97 review follow-ups (t13): scrubbing, SQL quoting, resume relink, verify_pack, builder main,
# embed fallback, renderer object branch, CLI citation line. Synthetic names only.
verify = _load("verify_pack")


def test_scrub_applies_to_every_search_text_component():
    pii = "kim@example.com"
    ph = "010-1234-5678"
    rows = [row(f"Root/{pii}/{ph}/A-101 {pii} plan.dwg", md5="d1", project=f"Root/{pii}")]
    rec = _rec(rows[0]["path"], [(f"LAY-{pii}", "architecture", 5)], [(f"BLK-{ph}", 2)])
    rec["blocks"][0]["category"] = pii
    rec["blocks"][0]["attrs"] = [ph]
    g = b.build_graph(rows, {rows[0]["path"]: rec})
    assert {n["type"] for n in g["nodes"].values()} >= {"SubProject", "Drawing", "LayerStandard", "BlockSpec"}
    for n in g["nodes"].values():
        assert "@" not in n["search_text"] and ph not in n["search_text"], n["type"]
    assert all("@" not in v["text"] and ph not in v["text"] for v in g["vectors"])
    # identity props keep the real path: only the embedded / searchable text is scrubbed
    drw = next(n for n in g["nodes"].values() if n["type"] == "Drawing")
    assert pii in drw["props"]["drive_path"]


def test_sql_quote_and_backslash_escaping_contract():
    assert b.q("O'Brien") == "'O''Brien'"
    assert b.q(None) == "NULL" and b.q(True) == "true" and b.q(7) == "7"
    # a backslash switches to an E'' literal, which is independent of standard_conforming_strings
    assert b.q("a\\b") == "E'a\\\\b'"
    assert b.q("it's a\\b") == "E'it''s a\\\\b'"


def test_render_sql_escapes_tricky_synthetic_names():
    name = "O'Brien\\Sub/A-101 it's.dwg"
    rows = [row(name, md5="d1", project="O'Brien\\Sub")]
    g = b.build_graph(rows, {})
    sql = b.render_sql(g["nodes"], g["edges"], g["aliases"])
    assert "O''Brien" in sql and "it''s" in sql
    assert "E'" in sql  # backslash-bearing values are E'' literals
    assert "O'Brien" not in sql.replace("O''Brien", "") and "it's" not in sql.replace("it''s", "")
    for stmt in sql.splitlines():
        if stmt.startswith("--"):
            continue
        # doubled quotes are escapes; what remains must be delimiter pairs only
        assert stmt.replace("''", "").count("'") % 2 == 0, stmt[:120]


def test_link_sql_handles_null_document_and_unlink_clears_dangling():
    assert "CASE WHEN o.document_id IS NULL THEN '{}'" in loader.LINK_SQL
    assert "CASE WHEN o.document_id IS NULL THEN '{}'" in b.render_sql({}, [], [])
    assert "NOT EXISTS (SELECT 1 FROM aec.objects o WHERE o.id = n.id)" in loader.UNLINK_SQL
    assert "n.project_key = 'GDRIVE_CAD'" in loader.UNLINK_SQL and "object_ids = '{}'" in loader.UNLINK_SQL


def test_relink_prunes_then_unlinks_then_links():
    log = []
    loader.relink(FakeCursor(log), [_vec(1)])
    sqls = [s for s, _ in log]
    assert "DELETE FROM aec.objects" in sqls[1]
    assert sqls[-2] == " ".join(loader.UNLINK_SQL.split()) and sqls[-1] == " ".join(loader.LINK_SQL.split())
    with pytest.raises(ValueError):  # an empty corpus never reaches the link statements
        loader.relink(FakeCursor([]), [])


def test_apply_skip_kg_resume_still_reconciles_links(monkeypatch, tmp_path):
    """--skip-kg skips the kg SQL (which would reset object_ids) but must still unlink pruned objects."""
    import types

    log = []

    class Cur(FakeCursor):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return Cur(log)

        def commit(self):
            pass

    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=lambda dsn: Conn()))
    pack = tmp_path / "pack"
    (pack / "graph").mkdir(parents=True)
    (pack / "vectors").mkdir()
    (pack / "sql").mkdir()
    (pack / "graph" / "kg_nodes.jsonl").write_text('{"id":"GDRIVE_CAD:x:1"}\n', encoding="utf-8")
    (pack / "vectors" / "vector_corpus.jsonl").write_text(json.dumps(_vec(1)) + "\n", encoding="utf-8")
    (pack / "sql" / loader.SQL_FILE).write_text("SELECT 'kg-sql-marker';", encoding="utf-8")
    monkeypatch.setattr(loader, "PACK", pack)
    monkeypatch.setattr(loader, "embed_many", lambda texts: [[0.0] * 1024 for _ in texts])
    assert loader.apply("dsn", True, 8) == 0
    sqls = [s for s, _ in log]
    assert not any("kg-sql-marker" in s for s in sqls)
    assert sqls[-1] == " ".join(loader.LINK_SQL.split()) and sqls[-2] == " ".join(loader.UNLINK_SQL.split())
    log.clear()
    assert loader.apply("dsn", False, 8) == 0
    assert any("kg-sql-marker" in s for s, _ in log)


# ---- embed_many fallback ---------------------------------------------------------------------------
def _http_error(code):
    import urllib.error

    return urllib.error.HTTPError("http://x", code, "err", {}, None)


def test_embed_many_falls_back_to_legacy_endpoint_on_404(monkeypatch):
    calls = []

    def fake_post(url, body, timeout=300):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/api/embed"):
            raise _http_error(404)
        return {"embedding": [0.5] * 1024}

    monkeypatch.setattr(loader, "post_json", fake_post)
    vecs = loader.embed_many(["a", "b"])
    assert calls == ["embed", "embeddings", "embeddings"] and len(vecs) == 2 and len(vecs[0]) == 1024


def test_embed_many_batch_path_and_error_handling(monkeypatch):
    import urllib.error

    monkeypatch.setattr(loader, "post_json", lambda url, body, timeout=300: {"embeddings": [[0.0] * 1024] * 2})
    assert len(loader.embed_many(["a", "b"])) == 2
    monkeypatch.setattr(loader, "post_json", lambda url, body, timeout=300: {"embeddings": [[0.0] * 1024]})
    with pytest.raises(RuntimeError):  # wrong number of vectors
        loader.embed_many(["a", "b"])

    def boom(url, body, timeout=300):
        raise _http_error(500)

    monkeypatch.setattr(loader, "post_json", boom)
    with pytest.raises(urllib.error.HTTPError):  # only 404 triggers the fallback
        loader.embed_many(["a"])
    monkeypatch.setattr(loader, "post_json", lambda url, body, timeout=300: {"embeddings": [[0.0] * 3]})
    with pytest.raises(RuntimeError):  # wrong dimension
        loader.embed_many(["a"])


# ---- builder main(): UP TO DATE / --force ----------------------------------------------------------
def _src(tmp_path, n=40):
    src = tmp_path / "src"
    src.mkdir()
    rows = [row(f"루트/프로젝트/건축/A-{i:03d} 배치도 평면도 입면도 단면도 계단 창호.dwg", md5=f"d{i}",
                project="루트/프로젝트") for i in range(1, n + 1)]
    (src / "cad_all.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return src


def _run_main(mod, monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["prog", *argv])
    return mod.main()


def test_builder_main_up_to_date_and_force(tmp_path, monkeypatch, capsys):
    src, out = _src(tmp_path), tmp_path / "out"
    assert _run_main(b, monkeypatch, ["--src", str(src), "--out", str(out)]) == 0
    first = (out / "manifest.json").read_text(encoding="utf-8")
    capsys.readouterr()
    assert _run_main(b, monkeypatch, ["--src", str(src), "--out", str(out)]) == 0
    assert "UP TO DATE" in capsys.readouterr().out
    assert (out / "manifest.json").read_text(encoding="utf-8") == first  # not rewritten
    assert _run_main(b, monkeypatch, ["--src", str(src), "--out", str(out), "--force"]) == 0
    rebuilt = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert "UP TO DATE" not in capsys.readouterr().out
    assert rebuilt["input_fingerprint"] == json.loads(first)["input_fingerprint"]
    # changed input invalidates the fingerprint
    rows = json.loads((src / "cad_all.json").read_text(encoding="utf-8"))
    (src / "cad_all.json").write_text(json.dumps(rows[:-1], ensure_ascii=False), encoding="utf-8")
    assert _run_main(b, monkeypatch, ["--src", str(src), "--out", str(out)]) == 0
    assert "UP TO DATE" not in capsys.readouterr().out
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["counts"]["drawings"] == len(rows) - 1


def test_builder_main_blocks_without_inputs(tmp_path, monkeypatch, capsys):
    assert _run_main(b, monkeypatch, ["--src", str(tmp_path / "nope"), "--out", str(tmp_path / "o")]) == 2
    assert "BLOCKED" in capsys.readouterr().err


# ---- verify_pack.py --------------------------------------------------------------------------------
def _verify(monkeypatch, capsys, out):
    monkeypatch.setattr(sys, "argv", ["verify", "--out", str(out)])
    code = verify.main()
    return code, json.loads(capsys.readouterr().out)


def _built_pack(tmp_path, monkeypatch, capsys, n=40):
    src, out = _src(tmp_path, n), tmp_path / "out"
    assert _run_main(b, monkeypatch, ["--src", str(src), "--out", str(out)]) == 0
    capsys.readouterr()
    return out


def test_verify_pack_accepts_a_clean_synthetic_pack(tmp_path, monkeypatch, capsys):
    out = _built_pack(tmp_path, monkeypatch, capsys)
    code, rep = _verify(monkeypatch, capsys, out)
    assert (code, rep["findings"]) == (0, ["NONE"])
    assert rep["nodes"] > 40


def _rewrite(path, fn):
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    fn(rows)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _drawing(rows):
    return next(n for n in rows if n["type"] == "Drawing")


def _manifest_count(o):
    m = json.loads((o / "manifest.json").read_text(encoding="utf-8"))
    m["counts"]["kg_nodes"] = 1
    (o / "manifest.json").write_text(json.dumps(m), encoding="utf-8")


@pytest.mark.parametrize("mutate, needle", [
    (lambda o: _rewrite(o / "graph" / "kg_edges.jsonl", lambda r: r.append({**r[0], "dst": "missing"})),
     "unresolved dst"),
    (lambda o: _rewrite(o / "graph" / "kg_nodes.jsonl", lambda r: r.append(dict(r[0]))), "duplicate node ids"),
    (lambda o: _rewrite(o / "vectors" / "vector_corpus.jsonl", lambda r: r[0].__setitem__("content_hash", "0" * 64)),
     "content_hash"),
    (lambda o: _rewrite(o / "graph" / "kg_nodes.jsonl",
                        lambda r: r[1].__setitem__("search_text", r[1]["search_text"] + " a@b.co")), "e-mail"),
    (lambda o: _rewrite(o / "graph" / "kg_nodes.jsonl",
                        lambda r: _drawing(r)["props"].__setitem__("discipline", "WIZARD")), "unknown discipline"),
    (lambda o: _rewrite(o / "graph" / "kg_nodes.jsonl",
                        lambda r: _drawing(r)["props"].__setitem__("review", True)), "review flag"),
    (_manifest_count, "manifest kg_nodes"),
])
def test_verify_pack_reports_corruption(tmp_path, monkeypatch, capsys, mutate, needle):
    out = _built_pack(tmp_path, monkeypatch, capsys)
    mutate(out)
    code, rep = _verify(monkeypatch, capsys, out)
    assert code == 1 and any(needle in f for f in rep["findings"]), rep["findings"]


def test_verify_pack_flags_too_little_hangul(tmp_path, monkeypatch, capsys):
    out = _built_pack(tmp_path, monkeypatch, capsys, n=1)
    code, rep = _verify(monkeypatch, capsys, out)
    assert code == 1 and any("Hangul" in f for f in rep["findings"])


# ---- ask.py object-branch renderer + commands.py citation line -------------------------------------
def _hit(props):
    from aec_intelligence.operational.search import Citation, SearchHit

    cit = Citation(document_id="doc1", document_name="DOC", revision=0, layout_or_page="p1", handle_or_id="h",
                   coordinate_system="CAD_WCS")
    return SearchHit(object_id="obj1", project_id="P", kind="drawing", label="SAMPLE", discipline="ARCH",
                     storey="", revision=0, score=0.99, lexical_score=0.5, vector_score=0.9, citation=cit,
                     properties=props)


def _semantic_items(props_list):
    from types import SimpleNamespace

    from aec_intelligence.operational.graphrag import ask

    class Stub:
        def search(self, question, **kw):
            return SimpleNamespace(hits=[_hit(p) for p in props_list], warnings=[])

    rag = ask.GraphRAG(db=None, settings=None, search_router=Stub())
    conn = SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(fetchall=lambda: []))
    return rag._semantic(conn, "q", None, budget_ms=None)


def test_semantic_object_branch_text_is_append_only_for_pack_hits():
    plain, rich = _semantic_items([{}, {"drive_path": "Root/ProjA/x/X-1 Sample.dwg", "sheet_number": "X-1",
                                         "title": "Sample title", "sub_project": "Root/ProjA"}])
    assert "경로" not in plain.text and plain.path is None
    assert rich.text.startswith(plain.text)  # other projects' text is a strict prefix: unchanged
    assert "도면번호 X-1, 제목 Sample title" in rich.text and "프로젝트 ProjA" in rich.text
    assert rich.text.endswith("경로 Root/ProjA/x/X-1 Sample.dwg") and rich.path == "Root/ProjA/x/X-1 Sample.dwg"
    # pack hit without sheet number / title / sub_project still renders safely
    only = _semantic_items([{"drive_path": "a/b.dwg"}])[0]
    assert "도면번호 미상, 제목 미상" in only.text and "프로젝트" not in only.text and only.path == "a/b.dwg"


def test_cmd_ask_prints_path_only_for_pack_citations(monkeypatch, capsys):
    from types import SimpleNamespace

    from aec_intelligence.operational.graphrag import ask, commands

    result = {"answer": "ans", "route": "r", "retrieval_ms": 1, "llm_ms": 0, "citations": [
        {"id": 1, "document_name": "D1", "layout_or_page": "p", "object_ids": ["o" * 20], "path": "Root/A/x.dwg"},
        {"id": 2, "document_name": "D2", "layout_or_page": None, "object_ids": []}]}

    class FakeRag:
        def __init__(self, *a, **k):
            pass

        def ask(self, *a, **k):
            return result

    monkeypatch.setattr(ask, "GraphRAG", FakeRag)
    parsed = SimpleNamespace(question="q", project="P", top_k=3, no_llm=True, json=False)
    commands.cmd_ask(parsed, None, None)
    lines = capsys.readouterr().out.splitlines()
    assert lines[2].startswith("[1] D1 | p | objects " + "o" * 16) and lines[2].endswith(" | path Root/A/x.dwg")
    assert lines[3] == "[2] D2 | - | objects -"
