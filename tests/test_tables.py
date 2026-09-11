import json
from pathlib import Path

from aec_intelligence.cair import CAIRObject, CAIRSnapshot, SourceRef
from aec_intelligence.tables import write_portable_tables


def test_portable_tables_are_jsonl_canonical_and_geometry_separate(tmp_path: Path):
    snapshot = CAIRSnapshot("P-TABLES", objects=[CAIRObject("aec://object/1", "P-TABLES", "Wall", SourceRef("a.dxf", "DXF", "A1"), geometry_ref="aec://geometry/1")])
    export = write_portable_tables(snapshot, tmp_path, geometry_rows=[{"geometry_ref": "aec://geometry/1", "handle": "A1", "geometry": {"kind": "line"}, "bbox": {"min_x": 0}}])
    assert export.format in {"JSONL", "JSONL+PARQUET"}
    assert export.files["objects.jsonl"].is_file()
    assert export.files["properties.jsonl"].is_file()
    assert export.files["source_mappings.jsonl"].is_file()
    assert export.files["application_mappings.jsonl"].is_file()
    object_row = json.loads((tmp_path / "objects.jsonl").read_text(encoding="utf-8").splitlines()[0])
    geometry_row = json.loads((tmp_path / "geometry_index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert object_row["geometry_ref"] == "aec://geometry/1"
    assert "geometry_json" not in object_row
    assert geometry_row["geometry_ref"] == "aec://geometry/1"
    manifest = json.loads((tmp_path / "table-manifest.json").read_text(encoding="utf-8"))
    assert manifest["canonical_format"] == "JSONL"
    assert manifest["tables"]["objects"]["row_count"] == 1
    assert manifest["tables"]["properties"]["row_count"] == 0
    assert manifest["tables"]["application_mappings"]["row_count"] == 3
