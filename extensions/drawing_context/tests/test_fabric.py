import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from context_fabric.adapters import adapt_snapshot, ragflow_projection
from context_fabric.catalog import Catalog
from context_fabric.contracts import SourceRevision, EmbeddingSpace, file_hash, verify_live_candidate
from context_fabric.planning import Capabilities, plan_ingestion, reciprocal_rank_fusion, typed_candidate_space, resolve_typed_choice


def source(**kwargs):
    return SourceRevision(**({"account": "owner", "corpus": "my-drive", "file_id": "file-1",
        "project_id": "P1", "revision": "1", "sha256": "a" * 64, "name": "공장.dwg",
        "format": "dwg", "parser": "fixture", "parser_version": "1", "units": "mm"} | kwargs))


def snapshot(src):
    return {"document_id": "doc", "project_id": src.project_id, "revision": src.revision,
        "source_hash": src.sha256, "units": src.units,
        "objects": [{"id": "door-1", "type": "Door", "state": "AI_INFERRED",
            "search_text": "공장 화장실 출입문 DOOR", "evidence": {
                "source_hash": src.sha256, "handle": "AB", "layout": "Model", "coordinate_system": "CAD_WCS"},
            "bbox": {"min_x": 0, "min_y": 0, "max_x": 900, "max_y": 100},
            "properties": {"width": 900}}], "relations": []}


class FabricTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = Catalog(Path(self.tmp.name) / "index.sqlite3")
        self.src = source()
        self.snap = snapshot(self.src)
        self.bundle = adapt_snapshot(self.snap, self.src)

    def test_snapshot_and_evidence_are_not_mutated(self):
        before = copy.deepcopy(self.snap)
        bundle = adapt_snapshot(self.snap, self.src)
        bundle["records"][0]["canonical_object"]["properties"]["width"] = 12
        self.assertEqual(before, self.snap)
        self.assertEqual(bundle["records"][0]["state"], "AI_INFERRED")

    def test_same_handle_in_different_files_never_collides(self):
        second = source(file_id="file-2")
        b = adapt_snapshot(snapshot(second), second)
        self.assertNotEqual(self.bundle["records"][0]["id"], b["records"][0]["id"])

    def test_bad_provenance_is_rejected(self):
        self.snap["objects"][0]["evidence"]["source_hash"] = "folder-name"
        with self.assertRaises(ValueError): adapt_snapshot(self.snap, self.src)

    def test_missing_layout_stays_unknown(self):
        del self.snap["objects"][0]["evidence"]["layout"]
        self.assertIsNone(adapt_snapshot(self.snap, self.src)["records"][0]["locator"]["layout"])

    def test_no_handle_from_generic_object_id(self):
        del self.snap["objects"][0]["evidence"]["handle"]
        self.assertIsNone(adapt_snapshot(self.snap, self.src)["records"][0]["locator"]["handle"])

    def test_idempotent_ingestion_and_lexical_search(self):
        self.catalog.ingest(self.bundle)
        self.catalog.ingest(self.bundle)
        hits = self.catalog.search("화장실", allowed_source_ids={self.src.source_id})
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["record"]["canonical_id"], "door-1")

    def test_immutable_conflict(self):
        self.catalog.ingest(self.bundle)
        self.bundle["records"][0]["text"] = "changed"
        with self.assertRaises(ValueError): self.catalog.ingest(self.bundle)

    def test_revision_swap_and_stale_result_blocking(self):
        self.catalog.ingest(self.bundle)
        next_source = source(revision="2", sha256="b" * 64)
        bundle = adapt_snapshot(snapshot(next_source), next_source)
        with self.assertRaises(ValueError): self.catalog.ingest(bundle)
        self.catalog.ingest(bundle, expected_current=self.src.revision_id)
        self.assertIsNone(self.catalog.get(self.bundle["records"][0]["id"], allowed_source_ids={self.src.source_id}))
        hits = self.catalog.search("출입문", allowed_source_ids={self.src.source_id})
        self.assertEqual([h["record"]["source"]["revision"] for h in hits], ["2"])

    def test_unauthorized_and_revoked_sources_hidden(self):
        self.catalog.ingest(self.bundle)
        self.assertEqual(self.catalog.search("화장실", allowed_source_ids=set()), [])
        self.catalog.revoke(self.src.source_id)
        self.assertEqual(self.catalog.search("화장실", allowed_source_ids={self.src.source_id}), [])
        self.assertEqual(self.catalog.export_current(allowed_source_ids={self.src.source_id}), [])
        with self.assertRaises(ValueError): self.catalog.ingest(self.bundle)

    def test_filter_before_limit(self):
        second = source(file_id="file-2", project_id="P2")
        self.catalog.ingest(adapt_snapshot(snapshot(second), second))
        self.catalog.ingest(self.bundle)
        hits = self.catalog.search("화장실", allowed_source_ids={self.src.source_id}, limit=1)
        self.assertEqual(hits[0]["record"]["source"]["project_id"], "P1")

    def test_fts_syntax_is_not_executed(self):
        self.catalog.ingest(self.bundle)
        self.catalog.search('" OR * NOT ()', allowed_source_ids={self.src.source_id})

    def test_embeddings_never_silently_mix(self):
        a = EmbeddingSpace("bge-m3", "commit-a", 1024, "l2", "text")
        b = replace(a, model="hash-fallback")
        with self.assertRaises(ValueError): a.require_same(b)
        a.require_same(a)

    def test_plan_requires_no_per_entity_mcp(self):
        p = plan_ingestion(self.src, Capabilities(oda=True, native2027=True), uncertain=True)
        self.assertEqual(p["backend"], "existing-oda-ezdxf")
        self.assertFalse(p["requires_mcp_per_entity"])
        self.assertEqual(p["jobs"][-1]["stage"], "native-review")

    def test_unchanged_skipped_and_unknown_formats_blocked(self):
        self.assertEqual(plan_ingestion(self.src, Capabilities(), self.src)["status"], "UNCHANGED")
        self.assertEqual(plan_ingestion(source(format="hwp"), Capabilities())["status"], "BLOCKED")

    def test_blob_dedup_does_not_dedup_identity(self):
        p = plan_ingestion(self.src, Capabilities(oda=True))
        q = plan_ingestion(source(file_id="another"), Capabilities(oda=True))
        self.assertEqual(p["parse_cache_key"], q["parse_cache_key"])
        self.assertNotEqual(p["jobs"], q["jobs"])

    def test_rrf_filters_untrusted_ids_and_duplicates(self):
        result = reciprocal_rank_fusion([["stale", "a", "a"], ["b", "a"]], {"a", "b"})
        self.assertEqual(result[0]["id"], "a")
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[0]["score"], 2 / 62)

    def test_typed_choice_only_allows_filtered_numbered_candidates(self):
        space = typed_candidate_space(
            [{"id": "stale", "score": 1.0}, {"id": "door-1", "score": 0.9}, {"id": "wall-1", "score": 0.8}],
            {"door-1", "wall-1"},
        )
        self.assertEqual([row["id"] for row in space["options"]], ["door-1", "wall-1"])
        selected = resolve_typed_choice(space, 1)
        self.assertEqual(selected["selected_id"], "door-1")
        self.assertFalse(selected["may_execute_mutation"])
        self.assertTrue(selected["requires_live_verification"])

    def test_typed_choice_rejects_generated_ids_and_out_of_range_options(self):
        space = typed_candidate_space(["door-1"], {"door-1"})
        with self.assertRaises(ValueError):
            resolve_typed_choice(space, "door-1")
        with self.assertRaises(ValueError):
            resolve_typed_choice(space, 2)

    def test_ragflow_export_retains_mapping(self):
        row = ragflow_projection(self.bundle["records"])[0]
        self.assertEqual(row["metadata"]["sha256"], self.src.sha256)
        self.assertEqual(row["external_id"], self.bundle["records"][0]["id"])

    def live(self):
        r = self.bundle["records"][0]
        return {**{k: r["locator"][k] for k in ["source_id", "layout", "handle", "instance_path"]},
                "revision": self.src.revision, "sha256": self.src.sha256,
                "native_mapping_verified": True, "document_dirty": False,
                "units": "mm", "fingerprint": "native-observation-123"}

    def test_live_validation_is_not_mutation_authorization(self):
        result = verify_live_candidate(self.bundle["records"][0], self.live())
        self.assertEqual(result["status"], "VERIFIED_FOR_REVIEW")
        self.assertFalse(result["may_execute_mutation"])

    def test_dirty_wrong_file_and_unmapped_handles_are_blocked(self):
        for patch in [{"sha256": "f" * 64}, {"document_dirty": True},
                      {"native_mapping_verified": False}, {"units": "inch"},
                      {"instance_path": ["DIFFERENT"]}, {"fingerprint": None}]:
            with self.subTest(patch=patch):
                result = verify_live_candidate(self.bundle["records"][0], self.live() | patch)
                self.assertEqual(result["status"], "REQUIRES_REVIEW")

    def test_cair_adapter_preserves_provenance(self):
        cair = {"schema_version": "0.1.0", "project_id": "P1", "objects": [{
            "id": "aec://object/1", "type": "Door", "source": {"entity_id": "AB", "layer": "DOOR"},
            "provenance": {"source_hash": self.src.sha256},
            "classification": {"state": "REQUIRES_REVIEW"}}], "relations": []}
        bundle = adapt_snapshot(cair, self.src)
        self.assertEqual(bundle["records"][0]["canonical_id"], "aec://object/1")
        self.assertIsNone(bundle["records"][0]["locator"]["layout"])

    def test_existing_ezdxf_parser_real_roundtrip_readonly(self):
        try:
            import ezdxf
        except ImportError:
            self.skipTest("Install existing Ontology cad extra for real parser integration")
        from aec_intelligence.operational.parsers import parse_source
        root = Path(self.tmp.name)
        original = root / "original.dxf"
        doc = ezdxf.new()
        doc.units = 4
        doc.modelspace().add_text("공장 화장실", dxfattribs={"layer": "TEXT"})
        doc.layouts.new("A101").add_text("1층 평면도")
        doc.saveas(original)
        checksum = file_hash(original)
        # Existing operational parser derives evidence hash from parent directory.
        staged = root / "captured" / checksum / "original.dxf"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(original.read_bytes())
        parsed = parse_source(staged, "doc", root / "derived", SimpleNamespace(data_root=root, oda_executable=""))
        src = source(format="dxf", sha256=checksum, units=parsed["units"])
        snap = {**parsed, "project_id": "P1", "document_id": "doc", "revision": "1", "source_hash": checksum}
        bundle = adapt_snapshot(snap, src)
        self.catalog.ingest(bundle)
        hits = self.catalog.search("화장실", allowed_source_ids={src.source_id})
        self.assertTrue(hits)
        self.assertEqual(hits[0]["record"]["locator"]["layout"], "Model")
        self.assertTrue(hits[0]["record"]["locator"]["handle"])
        self.assertEqual(checksum, file_hash(original))
        self.assertEqual(checksum, file_hash(staged))
        self.assertTrue(any(r["locator"]["layout"] == "A101" for r in bundle["records"]))


if __name__ == "__main__":
    unittest.main()
