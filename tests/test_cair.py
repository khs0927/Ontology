from aec_intelligence.cair import CAIR_SCHEMA_VERSION, CAIRSnapshot, Classification, SourceRef, stable_object_id


def test_stable_object_id_is_deterministic_and_source_scoped():
    first = stable_object_id("P1", "Wall", "DXF", "ABCD")
    assert first == stable_object_id("P1", "Wall", "DXF", "ABCD")
    assert first != stable_object_id("P2", "Wall", "DXF", "ABCD")
    assert first.startswith("aec://project/P1/wall/")


def test_snapshot_serializes_schema_and_status():
    snapshot = CAIRSnapshot("P1")
    assert snapshot.schema_version == CAIR_SCHEMA_VERSION
    assert snapshot.to_dict()["status"] == "SUCCESS"
    assert SourceRef("a.dxf", "DXF").to_dict()["entity_id"] is None
    assert Classification("Wall", 0.9, "rules").to_dict()["confidence"] == 0.9

