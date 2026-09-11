import json
from pathlib import Path

from aec_intelligence.pipeline import DXFIngestionPipeline
from aec_intelligence.retrieval import CrossProjectRetriever, write_memory_packages


FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"


def test_cross_project_retrieval_and_memory_packages(tmp_path: Path):
    pipeline = DXFIngestionPipeline(tmp_path)
    pipeline.ingest(FIXTURE, "P-RETRIEVE-A", "Courtyard School")
    pipeline.ingest(FIXTURE, "P-RETRIEVE-B", "Courtyard School Annex")
    retriever = CrossProjectRetriever(tmp_path)
    result = retriever.query_global_memory("Courtyard School Wall", top_k=5)
    assert result["route"] == "GLOBAL_MEMORY"
    assert result["hits"]
    similar = retriever.find_similar_projects("P-RETRIEVE-A")
    assert similar[0]["project_id"] == "P-RETRIEVE-B"
    outputs = write_memory_packages(tmp_path)
    assert outputs["global_context"].is_file()
    assert json.loads(outputs["P-RETRIEVE-A_context"].read_text(encoding="utf-8"))["object_count"] == 7
    assert outputs["P-RETRIEVE-A_summary"].is_file()
    assert outputs["P-RETRIEVE-A_active_design"].is_file()
    assert outputs["P-RETRIEVE-A_ontology_summary"].is_file()
    assert outputs["P-RETRIEVE-A_artifact_index"].is_file()
    assert outputs["project-summaries_jsonl"].is_file()
    assert outputs["lessons_jsonl"].is_file()
    assert (tmp_path / "global" / "07_DESIGN_KNOWLEDGE" / "lessons" / "lessons.jsonl").is_file()
    assert outputs["global_object_registry_jsonl"].is_file()
    assert outputs["source_mapping_jsonl"].is_file()
    assert outputs["vector_index_jsonl"].is_file()
    vector_rows = [json.loads(line) for line in outputs["vector_index_jsonl"].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert vector_rows
    assert all(len(row["vector"]) == 64 for row in vector_rows)
    vector_result = retriever.query_global_memory("concept Wall", top_k=1)
    assert vector_result["route"] == "VECTOR_PLUS_GRAPH"
    assert any("portable vector evidence" in reason for reason in vector_result["hits"][0]["reasons"])
