import hashlib
import unittest
from dataclasses import replace
from context_fabric.contracts import SourceRevision, digest
from context_fabric.source_handoff import build_source_handoff, validate_source_handoff


class SourceHandoffTests(unittest.TestCase):
    def setUp(self):
        self.content = b'headless fixture'
        self.source = SourceRevision('account', 'corpus', 'file', 'project', 'r1',
            hashlib.sha256(self.content).hexdigest(), 'drawing.dxf', 'dxf', 'ezdxf', '1', 'mm')

    def build(self, source=None):
        return build_source_handoff(source or self.source, self.content,
                                    capability_id='census', adapter_id='secondary/1')

    def test_roundtrip_is_non_authorizing_and_detached(self):
        handoff = self.build()
        validated = validate_source_handoff(handoff, self.source, self.content, capability_id='census', adapter_id='secondary/1')
        self.assertEqual(validated, handoff)
        self.assertEqual(handoff['native_validation'], 'NOT_RUN')
        self.assertFalse(handoff['execution_allowed'])
        self.assertFalse(handoff['acquisition_authenticated'])
        validated['source']['name'] = 'changed'
        self.assertNotEqual(validated, handoff)

    def test_parser_revision_separate_from_byte_revision(self):
        other = self.build(replace(self.source, parser_version='2'))
        self.assertEqual(self.build()['source_byte_revision_id'], other['source_byte_revision_id'])
        self.assertNotEqual(self.build()['parser_revision_id'], other['parser_revision_id'])

    def test_corrupt_bytes_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_source_handoff(self.build(), self.source, b'wrong', capability_id='census', adapter_id='secondary/1')

    def test_forged_authority_even_with_new_digest_is_rejected(self):
        for key, value in [('execution_allowed', True), ('canonical_allowed', True),
                           ('acquisition_authenticated', True), ('native_validation', 'VERIFIED')]:
            with self.subTest(key=key):
                handoff = self.build()
                handoff[key] = value
                handoff.pop('handoff_digest')
                handoff['handoff_digest'] = digest(handoff)
                with self.assertRaises(ValueError):
                    validate_source_handoff(handoff, self.source, self.content, capability_id='census', adapter_id='secondary/1')

    def test_wrong_source_membership_rejected(self):
        with self.assertRaises(ValueError):
            validate_source_handoff(self.build(), replace(self.source, project_id='other'), self.content, capability_id='census', adapter_id='secondary/1')

    def test_bad_identifier_and_nonbytes_rejected(self):
        for bad in ['', ' ', True, None, ' adapter ']:
            with self.assertRaises(ValueError):
                build_source_handoff(self.source, self.content, capability_id='census', adapter_id=bad)
        with self.assertRaises(ValueError):
            build_source_handoff(self.source, 'text', capability_id='census', adapter_id='v1')

    def test_receiver_pins_expected_adapter_scope(self):
        with self.assertRaises(ValueError):
            validate_source_handoff(self.build(), self.source, self.content,
                                    capability_id='other', adapter_id='secondary/1')
