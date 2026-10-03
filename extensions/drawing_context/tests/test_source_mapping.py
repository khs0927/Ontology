from __future__ import annotations

import unittest

from context_fabric.adapters import adapt_snapshot
from context_fabric.contracts import SourceRevision
from context_fabric.source_mapping import (
    LiveObjectObservation,
    TrustedSourceResolution,
    build_execution_evidence,
    build_executor_handoff,
    candidate_binding,
    source_byte_revision_id,
    verify_source_binding,
)


def source(**kwargs):
    values = {
        "account": "owner",
        "corpus": "my-drive",
        "file_id": "drive-file-1",
        "project_id": "P1",
        "revision": "drive-rev-7",
        "sha256": "a" * 64,
        "name": "A-201.dwg",
        "format": "dwg",
        "parser": "oda-ezdxf",
        "parser_version": "1.0",
        "units": "mm",
    }
    values.update(kwargs)
    return SourceRevision(**values)


def record(src: SourceRevision):
    snap = {
        "document_id": "parsed-doc-1",
        "project_id": src.project_id,
        "revision": src.revision,
        "source_hash": src.sha256,
        "units": src.units,
        "objects": [
            {
                "id": "door-1",
                "type": "Door",
                "state": "OBSERVED",
                "search_text": "SD-01 door",
                "evidence": {
                    "source_hash": src.sha256,
                    "handle": "2F3",
                    "layout": "Model",
                    "coordinate_system": "CAD_WCS",
                    "instance_path": ["10A", "2F3"],
                },
                "bbox": {"min_x": 0, "min_y": 0, "max_x": 900, "max_y": 100},
                "properties": {"mark": "SD-01"},
            }
        ],
        "relations": [],
    }
    return adapt_snapshot(snap, src)["records"][0]


def resolution(src: SourceRevision):
    return TrustedSourceResolution(
        source_id=src.source_id,
        source_byte_revision_id=source_byte_revision_id(src),
        resolved_path=r"C:\PowerCad\cache\A-201.dwg",
        resolved_sha256=src.sha256,
        resolver_id="drive-cache-resolver/1",
        resolver_issuer="sion-source-resolver",
        trust_domain="khs0927/aec-source-cache",
        signature_key_id="resolver-key-2026-10",
        receipt_signature_verified=True,
        cache_entry_id="cache-entry-A201-rev7",
        resolver_receipt_sha256="e" * 64,
        immutable_cache=True,
        resolved_at="2026-10-03T09:00:00+00:00",
    )


def live(src: SourceRevision, **kwargs):
    values = {
        "session_id": "acad-session-1",
        "document_id": "open-db-1",
        "native_path": r"c:/powercad/cache/a-201.dwg",
        "source_id": src.source_id,
        "source_byte_revision_id": source_byte_revision_id(src),
        "file_sha256": src.sha256,
        "state_digest": "state-123",
        "modification_generation": "generation-42",
        "revision": src.revision,
        "sha256": src.sha256,
        "layout": "Model",
        "handle": "2F3",
        "instance_path": ["10A", "2F3"],
        "native_mapping_verified": True,
        "document_dirty": False,
        "units": "mm",
        "fingerprint": "native-object-fingerprint",
    }
    values.update(kwargs)
    return LiveObjectObservation(**values)


class SourceMappingTests(unittest.TestCase):
    def test_source_byte_revision_is_distinct_from_parser_revision(self):
        first = source(parser_version="1")
        second = source(parser_version="2")
        self.assertEqual(source_byte_revision_id(first), source_byte_revision_id(second))
        self.assertNotEqual(first.revision_id, second.revision_id)

    def test_candidate_never_authorizes_execution(self):
        src = source()
        candidate = candidate_binding(record(src), src)
        self.assertEqual(candidate["binding_state"], "CANDIDATE")
        self.assertFalse(candidate["execution_authorized"])
        self.assertFalse(candidate["may_execute_mutation"])
        self.assertTrue(candidate["requires_executor_authorization"])

    def test_matching_source_resolution_and_live_object_becomes_source_bound(self):
        src = source()
        report = verify_source_binding(record(src), src, resolution(src), live(src))
        self.assertEqual(report["binding_state"], "SOURCE_BOUND")
        self.assertEqual(report["reasons"], [])
        self.assertEqual(report["review_guard"]["status"], "VERIFIED_FOR_REVIEW")
        self.assertFalse(report["execution_authorized"])

    def test_path_comparison_uses_windows_semantics_not_basename_only(self):
        src = source()
        same_case_variant = live(src, native_path=r"C:\POWERCAD\CACHE\A-201.DWG")
        ok = verify_source_binding(record(src), src, resolution(src), same_case_variant)
        self.assertEqual(ok["binding_state"], "SOURCE_BOUND")

        wrong_folder = live(src, native_path=r"D:\other\A-201.dwg")
        bad = verify_source_binding(record(src), src, resolution(src), wrong_folder)
        self.assertEqual(bad["binding_state"], "CANDIDATE")
        self.assertIn("native_path_mismatch", bad["reasons"])

    def test_same_handle_in_wrong_source_revision_is_not_bound(self):
        src = source()
        newer = source(revision="drive-rev-8", sha256="b" * 64)
        obs = live(
            src,
            source_byte_revision_id=source_byte_revision_id(newer),
            file_sha256=newer.sha256,
            sha256=newer.sha256,
            revision=newer.revision,
        )
        report = verify_source_binding(record(src), src, resolution(src), obs)
        self.assertEqual(report["binding_state"], "CANDIDATE")
        self.assertIn("live_source_byte_revision_mismatch", report["reasons"])
        self.assertIn("live_file_hash_mismatch", report["reasons"])

    def test_nested_instance_path_is_part_of_identity(self):
        src = source()
        report = verify_source_binding(
            record(src),
            src,
            resolution(src),
            live(src, instance_path=["DIFFERENT", "2F3"]),
        )
        self.assertEqual(report["binding_state"], "CANDIDATE")
        self.assertIn("live_instance_path_mismatch", report["reasons"])

    def test_verified_source_binding_builds_non_authorizing_executor_handoff(self):
        src = source()
        report = verify_source_binding(record(src), src, resolution(src), live(src))
        handoff = build_executor_handoff(report)

        self.assertEqual(handoff["schema"], "aec-executor-handoff/1")
        self.assertEqual(handoff["binding_state"], "SOURCE_BOUND")
        self.assertEqual(handoff["review_status"], "VERIFIED_FOR_REVIEW")
        self.assertEqual(handoff["document_id"], "open-db-1")
        self.assertEqual(handoff["source_id"], src.source_id)
        self.assertEqual(handoff["source_sha256"], src.sha256)
        self.assertEqual(handoff["file_sha256"], src.sha256)
        self.assertEqual(handoff["object_locator"]["handle"], "2F3")
        self.assertEqual(len(handoff["handoff_digest"]), 64)
        self.assertFalse(handoff["execution_authorized"])
        self.assertFalse(handoff["may_execute_mutation"])
        self.assertTrue(handoff["requires_executor_authorization"])

    def test_executor_handoff_hashes_are_part_of_handoff_digest(self):
        src = source()
        report = verify_source_binding(record(src), src, resolution(src), live(src))
        handoff = build_executor_handoff(report)
        changed = dict(report)
        changed["source_sha256"] = "b" * 64
        changed["live_document"] = dict(report["live_document"])
        changed["live_document"]["file_sha256"] = "b" * 64
        changed_handoff = build_executor_handoff(changed)
        self.assertNotEqual(handoff["handoff_digest"], changed_handoff["handoff_digest"])

    def test_committed_executor_receipt_builds_noncanonical_evidence(self):
        src = source()
        report = verify_source_binding(record(src), src, resolution(src), live(src))
        handoff = build_executor_handoff(report)
        receipt = {
            "executor": "power-cad",
            "plan_id": "plan-001",
            "document_id": handoff["document_id"],
            "source_binding_handoff_digest": handoff["handoff_digest"],
            "source_id": handoff["source_id"],
            "source_byte_revision_id": handoff["source_byte_revision_id"],
            "parser_revision_id": handoff["parser_revision_id"],
            "dry_run": False,
            "committed": True,
            "changes": [{"handle": "2F3", "status": "updated"}],
        }

        evidence = build_execution_evidence(handoff, receipt)

        self.assertEqual(evidence["schema"], "aec-execution-evidence/1")
        self.assertEqual(evidence["evidence_state"], "EXECUTED")
        self.assertEqual(evidence["executor"], "power-cad")
        self.assertEqual(evidence["document_id"], "open-db-1")
        self.assertEqual(evidence["handoff_digest"], handoff["handoff_digest"])
        self.assertEqual(len(evidence["receipt_digest"]), 64)
        self.assertTrue(evidence["committed"])
        self.assertFalse(evidence["canonical_mutation"])
        self.assertFalse(evidence["execution_authorized"])

    def test_executor_receipt_must_match_original_handoff(self):
        src = source()
        report = verify_source_binding(record(src), src, resolution(src), live(src))
        handoff = build_executor_handoff(report)
        receipt = {
            "executor": "power-cad",
            "plan_id": "plan-001",
            "document_id": "another-document",
            "source_binding_handoff_digest": handoff["handoff_digest"],
            "source_id": handoff["source_id"],
            "source_byte_revision_id": handoff["source_byte_revision_id"],
            "parser_revision_id": handoff["parser_revision_id"],
            "dry_run": False,
            "committed": True,
        }

        with self.assertRaisesRegex(ValueError, "document_id"):
            build_execution_evidence(handoff, receipt)

    def test_preview_receipt_is_not_execution_evidence(self):
        src = source()
        report = verify_source_binding(record(src), src, resolution(src), live(src))
        handoff = build_executor_handoff(report)
        receipt = {
            "executor": "power-cad",
            "plan_id": "plan-preview",
            "document_id": handoff["document_id"],
            "source_binding_handoff_digest": handoff["handoff_digest"],
            "source_id": handoff["source_id"],
            "source_byte_revision_id": handoff["source_byte_revision_id"],
            "parser_revision_id": handoff["parser_revision_id"],
            "dry_run": True,
            "committed": False,
        }

        with self.assertRaisesRegex(ValueError, "committed non-dry-run"):
            build_execution_evidence(handoff, receipt)

    def test_dirty_source_binding_cannot_cross_executor_handoff_gate(self):
        src = source()
        report = verify_source_binding(
            record(src),
            src,
            resolution(src),
            live(src, document_dirty=True),
        )
        self.assertEqual(report["binding_state"], "SOURCE_BOUND")
        with self.assertRaisesRegex(ValueError, "VERIFIED_FOR_REVIEW"):
            build_executor_handoff(report)

    def test_dirty_document_can_be_source_bound_but_not_review_ready(self):
        src = source()
        report = verify_source_binding(
            record(src),
            src,
            resolution(src),
            live(
                src,
                document_dirty=True,
                state_digest="state-dirty-124",
                modification_generation="generation-43",
            ),
        )
        self.assertEqual(report["binding_state"], "SOURCE_BOUND")
        self.assertEqual(report["review_guard"]["status"], "REQUIRES_REVIEW")
        self.assertIn("dirty_or_unknown_document", report["review_guard"]["reasons"])
        self.assertFalse(report["execution_authorized"])

    def test_resolver_requires_immutable_receipt_evidence(self):
        src = source()
        with self.assertRaises(ValueError):
            TrustedSourceResolution(
                source_id=src.source_id,
                source_byte_revision_id=source_byte_revision_id(src),
                resolved_path=r"C:\\PowerCad\\cache\\A-201.dwg",
                resolved_sha256=src.sha256,
                resolver_id="drive-cache-resolver/1",
                resolver_issuer="sion-source-resolver",
                trust_domain="khs0927/aec-source-cache",
                signature_key_id="resolver-key-2026-10",
                receipt_signature_verified=True,
                cache_entry_id="cache-entry-A201-rev7",
                resolver_receipt_sha256="e" * 64,
                immutable_cache=False,
                resolved_at="2026-10-03T09:00:00+00:00",
            )

    def test_unsigned_resolver_receipt_is_rejected(self):
        src = source()
        with self.assertRaises(ValueError):
            TrustedSourceResolution(
                source_id=src.source_id,
                source_byte_revision_id=source_byte_revision_id(src),
                resolved_path=r"C:\\PowerCad\\cache\\A-201.dwg",
                resolved_sha256=src.sha256,
                resolver_id="drive-cache-resolver/1",
                resolver_issuer="sion-source-resolver",
                trust_domain="khs0927/aec-source-cache",
                signature_key_id="resolver-key-2026-10",
                receipt_signature_verified=False,
                cache_entry_id="cache-entry-A201-rev7",
                resolver_receipt_sha256="e" * 64,
                immutable_cache=True,
                resolved_at="2026-10-03T09:00:00+00:00",
            )

    def test_missing_native_mapping_never_becomes_source_bound(self):
        src = source()
        report = verify_source_binding(
            record(src),
            src,
            resolution(src),
            live(src, native_mapping_verified=False),
        )
        self.assertEqual(report["binding_state"], "CANDIDATE")
        self.assertIn("native_mapping_not_verified", report["reasons"])

    def test_parser_revision_mismatch_is_detected_even_when_source_bytes_match(self):
        src = source()
        changed_parser = source(parser_version="2")
        report = verify_source_binding(
            record(src),
            changed_parser,
            resolution(changed_parser),
            live(changed_parser),
        )
        self.assertEqual(report["binding_state"], "CANDIDATE")
        self.assertIn("record_source_revision_mismatch", report["reasons"])
        self.assertIn("locator_parser_revision_mismatch", report["reasons"])


if __name__ == "__main__":
    unittest.main()
