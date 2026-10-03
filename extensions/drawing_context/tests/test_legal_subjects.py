from __future__ import annotations

import unittest

from context_fabric.legal_subjects import adapt_archontos_subject_ref


def subject(**updates):
    value = {
        "schema": "archontos-aec-subject-ref/1",
        "project_id": "P-001",
        "object_id": "door-1",
        "source_id": "a" * 64,
        "source_byte_revision_id": "b" * 64,
        "parser_revision_id": "c" * 64,
        "locator": {"layout": "Model", "handle": "2F3"},
    }
    value.update(updates)
    return value


class LegalSubjectTests(unittest.TestCase):
    def test_archontos_subject_becomes_non_authorizing_legal_evidence(self):
        row = adapt_archontos_subject_ref(subject())

        self.assertEqual(row["schema"], "drawing-context-legal-subject/1")
        self.assertEqual(row["source_schema"], "archontos-aec-subject-ref/1")
        self.assertEqual(row["project_id"], "P-001")
        self.assertEqual(row["object_id"], "door-1")
        self.assertTrue(row["legal_evidence_only"])
        self.assertFalse(row["canonical"])
        self.assertFalse(row["execution_authorized"])
        self.assertFalse(row["may_execute_mutation"])
        self.assertEqual(len(row["evidence_id"]), 64)

    def test_partial_source_revision_identity_is_rejected(self):
        bad = subject(source_byte_revision_id=None)

        with self.assertRaisesRegex(ValueError, "must be supplied together"):
            adapt_archontos_subject_ref(bad)

    def test_live_document_identity_cannot_enter_legal_subject_locator(self):
        bad = subject(locator={"layout": "Model", "handle": "2F3", "document_id": "open-db-1"})

        with self.assertRaisesRegex(ValueError, "live CAD"):
            adapt_archontos_subject_ref(bad)

    def test_subject_without_source_revision_can_still_reference_project_object(self):
        row = adapt_archontos_subject_ref(
            subject(
                source_id=None,
                source_byte_revision_id=None,
                parser_revision_id=None,
            )
        )

        self.assertEqual(row["project_id"], "P-001")
        self.assertEqual(row["object_id"], "door-1")
        self.assertIsNone(row["source_id"])


if __name__ == "__main__":
    unittest.main()
