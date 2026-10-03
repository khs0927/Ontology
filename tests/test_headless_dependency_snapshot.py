"""Check the bounded dependency snapshot, without acquiring other PRs."""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]

class DependencySnapshotTests(unittest.TestCase):
    def test_snapshot_is_bounded_and_authority_free(self):
        data = json.loads((ROOT / 'docs/HEADLESS_PR_DEPENDENCIES.json').read_text())
        self.assertEqual(data['schema_version'], 1)
        self.assertTrue(data['snapshot_only'])
        self.assertFalse(data['execution_allowed'])
        self.assertFalse(data['canonical_allowed'])
        self.assertEqual(data['native_validation'], 'NOT_RUN')
        self.assertEqual(data['original_asset_validation'], 'NOT_RUN')
        self.assertEqual({e['pr'] for e in data['entries']}, set(range(36, 41)))
        seen = set()
        for entry in data['entries']:
            self.assertRegex(entry['head'], r'^[0-9a-f]{40}$')
            self.assertEqual(entry['base'], data['baseline_commit'])
            self.assertEqual(entry['hard_pr_dependencies'], [])
            for path in entry['changed_paths']:
                self.assertNotIn(path, seen)
                seen.add(path)
            for path in entry['master_prerequisites']:
                self.assertTrue((ROOT / path).is_file(), path)
        for edge in data['optional_relationships']:
            self.assertEqual(edge['kind'], 'conceptual-only')

    def test_baseline_imported_symbols_exist(self):
        expected = {
            'extensions/drawing_context/context_fabric/contracts.py': ['SourceRevision', 'digest'],
            'extensions/drawing_context/context_fabric/source_mapping.py': ['source_byte_revision_id'],
            'extensions/external_capabilities/capability_registry/__init__.py': ['_hash', 'evidence_record', 'project_evidence'],
        }
        import ast
        for path, names in expected.items():
            tree = ast.parse((ROOT / path).read_text())
            declared = {node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))}
            self.assertTrue(set(names) <= declared, path)
