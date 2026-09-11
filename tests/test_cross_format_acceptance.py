from collections import Counter
from pathlib import Path
import json
import shutil

import pytest

from aec_intelligence.classifier import classify
from aec_intelligence.dwg import ODAConverter
from aec_intelligence.dxf import DXFParser
from aec_intelligence.formats import IFCParser, normalize_ifc_to_cair


def _cad_semantic_counts(path: Path) -> Counter[str]:
    parsed = DXFParser().parse(path)
    return Counter(classify(entity)[0] for entity in parsed.entities)


def test_dwg_dxf_ifc_fixture_mapping_preserves_shared_semantics(tmp_path: Path):
    fixture_root = Path(__file__).parents[1] / "fixtures"
    ground_truth = json.loads((fixture_root / "known-ground-truth.json").read_text(encoding="utf-8"))
    shared_semantics = {"Wall", "Door", "Window"}

    dxf_counts = _cad_semantic_counts(fixture_root / "simple_house.dxf")
    assert shared_semantics <= set(dxf_counts)
    assert dxf_counts["Wall"] == ground_truth["dxf"]["wall_count"]
    assert dxf_counts["Door"] == ground_truth["dxf"]["door_count"]

    ifc_result = IFCParser().parse(fixture_root / "simple_house.ifc")
    ifc_snapshot = normalize_ifc_to_cair(ifc_result, "P-CROSS-FORMAT")
    ifc_semantics = {obj.type for obj in ifc_snapshot.objects}
    assert shared_semantics <= ifc_semantics
    assert len(ifc_snapshot.relations) == ground_truth["ifc"]["aggregate_relation_count"]

    executable = shutil.which("ODAFileConverter") or Path(r"C:\Program Files\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe")
    if not Path(executable).is_file():
        pytest.skip("ODA File Converter is not configured; IFC/DXF comparison passed")
    converted = ODAConverter(executable).convert_to_dxf(fixture_root / "simple_house.dwg", tmp_path / "dwg")
    assert converted.status == "SUCCESS"
    dwg_counts = _cad_semantic_counts(Path(converted.output))
    assert shared_semantics <= set(dwg_counts)
    assert dwg_counts["Wall"] > 0
    assert dwg_counts["Door"] > 0
    assert dwg_counts["Window"] > 0

