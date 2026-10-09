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
