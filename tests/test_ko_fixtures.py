"""Korean drawing fixtures: generation, golden-label integrity and the eval harness."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
COMMITTED = ROOT / "tests" / "fixtures" / "drawings_ko"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


make = _load("make_ko_fixtures")
evalmod = _load("eval_classification")


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("drawings_ko")
    labels = make.generate(out)
    return out, labels


def test_fixture_set_covers_required_sheets_and_classes(generated):
    _, labels = generated
    numbers = {f["sheet"]["number"] for f in labels["files"].values()}
    assert {"A-101", "A-501", "A-601", "S-301", "S-201"} <= numbers
    categories = {f["sheet"]["category"] for f in labels["files"].values()}
    assert {"plan", "detail", "structural", "elevation", "section", "schedule"} <= categories
    classes = {e["class"] for f in labels["files"].values() for e in f["entities"] if e.get("score", True)}
    assert classes == set(make.ENTITY_CLASSES)


def test_every_file_opens_and_every_modelspace_entity_is_labelled(generated):
    out, labels = generated
    for filename, golden in labels["files"].items():
        doc = ezdxf.readfile(out / filename)
        assert not doc.audit().has_errors, filename
        msp = {e.dxf.handle: e for e in doc.modelspace()}
        labelled = {e["handle"]: e for e in golden["entities"]}
        assert set(msp) == set(labelled), filename
        for handle, lab in labelled.items():
            entity = msp[handle]
            assert entity.dxftype() == lab["entity_type"], (filename, handle)
            assert entity.dxf.layer == lab["layer"], (filename, handle)
            if lab["entity_type"] == "INSERT":
                assert entity.dxf.name == lab["block"], (filename, handle)
                assert lab["block"] in doc.blocks
            if lab.get("score", True):
                assert lab["class"] in make.ENTITY_CLASSES
        block_names = {b.name for b in doc.blocks if not b.name.lower().startswith(("*model_space", "*paper_space"))}
        assert block_names == {b["name"] for b in golden["blocks"]}, filename


def test_title_blocks_carry_sheet_metadata(generated):
    out, labels = generated
    for filename, golden in labels["files"].items():
        doc = ezdxf.readfile(out / filename)
        titles = [doc.entitydb[e["handle"]] for e in golden["entities"] if e["class"] == "TitleBlock"]
        assert len(titles) == 1, filename
        attribs = {a.dxf.tag: a.dxf.text for a in titles[0].attribs}
        assert attribs["도면번호"] == golden["sheet"]["number"]
        assert attribs["도면명"] == golden["sheet"]["title"]
        assert attribs["축척"] == golden["sheet"]["scale"]
        assert attribs["개정"] == golden["sheet"]["revision"]


def test_korean_block_names_attributes_and_special_blocks(generated):
    out, labels = generated
    plan = labels["files"]["A-101_1층평면도.dxf"]
    blocks = {b["name"]: b for b in plan["blocks"]}
    for name in ("D1", "DOOR_900", "문-편개", "SD-01", "AD", "W1", "WIN_1500", "창-미서기", "AW-02", "PW",
                 "변기", "세면대", "TOILET", "SINK", "UNIT_화장실", "방위표", "*U12", "X-BASE"):
        assert name in blocks, name
    assert blocks["*U12"]["category"] == "Door" and blocks["*U12"]["anonymous"]
    doc = ezdxf.readfile(out / "A-101_1층평면도.dxf")
    assert doc.blocks.get("X-BASE").block.is_xref
    nested = [e.dxf.name for e in doc.blocks.get("UNIT_화장실") if e.dxftype() == "INSERT"]
    assert nested == ["변기", "세면대", "욕조"]
    door = next(e for e in plan["entities"] if e.get("block") == "D1")
    assert door["attribs"] == {"DOOR_NO": "D1", "규격": "900x2100", "W": "900", "H": "2100"}
    texts = {e.get("text") for e in plan["entities"]}
    assert {"거실", "침실1", "화장실", "현관", "발코니"} <= texts
    assert any(t and t.endswith("㎡") for t in texts)
    steel = {e.get("text") for e in labels["files"]["S-301_접합부상세도.dxf"]["entities"]}
    assert {"H-300x300x10x15", "C-100x50x5x7.5", "L-75x75x6", "4-M20 F10T (HTB)"} <= steel


def test_cp949_r2000_file_is_really_cp949(generated):
    out, labels = generated
    name = "A-102_2층평면도_cp949.dxf"
    assert labels["files"][name]["encoding"] == "cp949"
    raw = (out / name).read_bytes()
    assert b"ANSI_949" in raw
    assert "거실".encode("cp949") in raw
    assert "거실".encode("utf-8") not in raw
    doc = ezdxf.readfile(out / name)
    assert doc.dxfversion == "AC1015"
    assert "거실" in {e.dxf.text for e in doc.modelspace().query("TEXT")}


def test_generation_matches_committed_golden_labels(generated):
    _, labels = generated
    committed = COMMITTED / "labels.json"
    if not committed.exists():
        pytest.skip("committed fixtures not present")
    golden = json.loads(committed.read_text(encoding="utf-8"))
    golden.pop("ezdxf_version", None)
    fresh = json.loads(json.dumps(labels, ensure_ascii=False))
    fresh.pop("ezdxf_version", None)
    assert fresh == golden, "fixtures drifted: rerun scripts/make_ko_fixtures.py and commit"


def test_eval_harness_degrades_when_parser_is_missing(generated, tmp_path, monkeypatch):
    out, labels = generated

    def broken(_src):
        raise ImportError("no parser")

    monkeypatch.setattr(evalmod, "_import_parser", broken)
    report = evalmod.evaluate(labels, out, None)
    assert report["entities"]["total"] > 0
    assert report["entities"]["unclassified"] == report["entities"]["total"]
    assert report["sheets"]["metadata_exposed"] == 0
    assert evalmod.map_prediction(None) is None
    assert evalmod.map_prediction({"type": "CADEntity"}) is None
    assert evalmod.map_prediction({"type": "Door", "properties": {"semantic_class": "Window"}}) == "Window"


def test_eval_harness_runs_against_repository_parser(generated, tmp_path):
    pytest.importorskip("aec_intelligence.dxf")
    out, labels = generated
    target = tmp_path / "report.json"
    assert evalmod.main([str(target), "--fixtures", str(out), "--quiet"]) == 0
    report = json.loads(target.read_text(encoding="utf-8"))
    assert report["entities"]["total"] == sum(
        1 for f in labels["files"].values() for e in f["entities"] if e.get("score", True)
    )
    assert set(report["entities"]["per_class"]) >= {"Door", "Window", "Wall"}
    assert all(f["missing_from_parser_output"] == 0 for f in report["files"].values())
