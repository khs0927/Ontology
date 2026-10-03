from __future__ import annotations

import unittest

from context_fabric.derived_artifacts import adapt_sketcharch_export_manifest


def manifest(**updates):
    value = {
        "schema": "sketcharch-drawing-export/1",
        "project_id": "P-SKETCH-01",
        "source_model_fingerprint": "b" * 64,
        "source_model_revision": "skp-rev-42",
        "ontology_ingest_mode": "DERIVED_ARTIFACT",
        "canonical_mutation": False,
        "artifacts": [
            {
                "artifact_id": "SEC-A-001",
                "view_kind": "section",
                "format": "dxf",
                "relative_path": "exports/sections/A-001.dxf",
                "sha256": "a" * 64,
                "source_model_fingerprint": "b" * 64,
                "source_element_ids": ["A-WALL-01", "A-SLAB-01"],
                "units": "mm",
                "derived": True,
            },
            {
                "artifact_id": "ELV-A-001",
                "view_kind": "elevation",
                "format": "svg",
                "relative_path": "exports/elevations/A-001.svg",
                "sha256": "c" * 64,
                "source_model_fingerprint": "b" * 64,
                "source_element_ids": [],
                "units": "mm",
                "derived": True,
            },
        ],
    }
    value.update(updates)
    return value


class DerivedArtifactTests(unittest.TestCase):
    def test_sketcharch_manifest_becomes_noncanonical_derived_evidence(self):
        bundle = adapt_sketcharch_export_manifest(manifest())

        self.assertEqual(bundle["schema"], "drawing-context-derived-artifacts/1")
        self.assertEqual(bundle["source_schema"], "sketcharch-drawing-export/1")
        self.assertEqual(len(bundle["records"]), 2)
        self.assertFalse(bundle["canonical_mutation"])
        self.assertFalse(bundle["execution_authorized"])
        for row in bundle["records"]:
            self.assertTrue(row["derived"])
            self.assertFalse(row["canonical"])
            self.assertFalse(row["execution_authorized"])
            self.assertEqual(len(row["id"]), 64)

    def test_manifest_cannot_promote_derived_export_to_canonical_mutation(self):
        with self.assertRaisesRegex(ValueError, "canonical mutation"):
            adapt_sketcharch_export_manifest(manifest(canonical_mutation=True))

    def test_artifact_must_match_source_model_fingerprint(self):
        bad = manifest()
        bad["artifacts"][0]["source_model_fingerprint"] = "d" * 64

        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            adapt_sketcharch_export_manifest(bad)

    def test_artifact_path_cannot_escape_export_root(self):
        bad = manifest()
        bad["artifacts"][0]["relative_path"] = "../outside.dxf"

        with self.assertRaisesRegex(ValueError, "export root"):
            adapt_sketcharch_export_manifest(bad)

    def test_duplicate_artifact_paths_are_rejected_case_insensitively(self):
        bad = manifest()
        bad["artifacts"][1]["relative_path"] = "EXPORTS/SECTIONS/A-001.DXF"

        with self.assertRaisesRegex(ValueError, "paths must be unique"):
            adapt_sketcharch_export_manifest(bad)


if __name__ == "__main__":
    unittest.main()
