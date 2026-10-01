import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from intelligence_fabric.code import diff_code_snapshots, refresh_code_snapshot, save_code_snapshot, scan_code_tree
from intelligence_fabric.graph import HydraDBConfig, build_hydradb_seed, render_hydradb_seed
from intelligence_fabric.jev import JevGrepAdapter
from intelligence_fabric.planning import GraphRequirements, choose_graph_backend, intelligence_plan


class IntelligenceFabricTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        global_root = self.root / "global" / "00_GLOBAL"
        global_root.mkdir(parents=True)
        rows = {
            "global-project-registry.jsonl": [{"project_id": "P1", "name": "Factory", "status": "ACTIVE"}],
            "global-object-registry.jsonl": [
                {"id": "aec://object/door-1", "project_id": "P1", "type": "Door", "classification": {"confidence": 0.9}, "geometry_ref": "geom://1"},
                {"id": "aec://object/wall-1", "project_id": "P1", "type": "Wall", "classification": {"confidence": 1.0}, "geometry_ref": "geom://2"},
            ],
            "global-relations.jsonl": [
                {"subject": "aec://object/door-1", "predicate": "HOSTED_BY", "object": "aec://object/wall-1", "confidence": 0.8}
            ],
            "global-provenance.jsonl": [
                {"object_id": "aec://object/door-1", "source_file": "factory.dwg", "source_hash": "a" * 64}
            ],
        }
        for name, values in rows.items():
            (global_root / name).write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values), encoding="utf-8")

    def test_code_snapshot_is_runtime_only_and_incremental(self):
        src = self.root / "src"
        src.mkdir()
        a = src / "a.py"
        b = src / "b.cs"
        a.write_text("print('a')\n", encoding="utf-8")
        b.write_text("class B {}\n", encoding="utf-8")

        first = refresh_code_snapshot(self.root)
        self.assertEqual(first["status"], "SUCCESS")
        self.assertTrue(first["diff"]["changed"])
        self.assertEqual(sorted(first["diff"]["added"]), ["src/a.py", "src/b.cs"])
        snapshot_path = Path(first["snapshot"])
        self.assertTrue(snapshot_path.is_file())
        self.assertTrue(str(snapshot_path).startswith(str(self.root / "runtime" / "code-intelligence")))
        self.assertFalse((self.root / "global" / "code-intelligence").exists())

        second = refresh_code_snapshot(self.root)
        self.assertFalse(second["diff"]["changed"])
        self.assertEqual(second["diff"]["unchanged"], 2)

        a.write_text("print('changed')\n", encoding="utf-8")
        b.unlink()
        (src / "c.py").write_text("print('c')\n", encoding="utf-8")
        third = refresh_code_snapshot(self.root)
        self.assertEqual(third["diff"]["modified"], ["src/a.py"])
        self.assertEqual(third["diff"]["deleted"], ["src/b.cs"])
        self.assertEqual(third["diff"]["added"], ["src/c.py"])
        self.assertTrue(third["diff"]["derived_code_graph_stale"])

    def test_code_snapshot_records_filesystem_provenance(self):
        src = self.root / "src"
        src.mkdir()
        (src / "x.py").write_text("x = 1\n", encoding="utf-8")
        snapshot = scan_code_tree(self.root)
        self.assertEqual(len(snapshot.files), 1)
        self.assertEqual(snapshot.files[0].provenance, "filesystem")
        self.assertEqual(snapshot.files[0].evidence, "EXTRACTED")
        diff = diff_code_snapshots(None, snapshot)
        self.assertFalse(diff["canonical"])
        self.assertEqual(diff["source_of_truth"], "filesystem")

    def test_code_snapshot_cannot_escape_runtime_directory(self):
        src = self.root / "src"
        src.mkdir()
        (src / "x.py").write_text("x = 1\n", encoding="utf-8")
        snapshot = scan_code_tree(self.root)
        for target in [self.root / "global" / "bad.json", self.root / "outside.json"]:
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    save_code_snapshot(self.root, snapshot, target)

    def test_code_snapshot_skips_oversized_files_with_warning(self):
        src = self.root / "src"
        src.mkdir()
        (src / "small.py").write_text("x = 1\n", encoding="utf-8")
        (src / "large.py").write_text("0123456789", encoding="utf-8")
        snapshot = scan_code_tree(self.root, max_file_bytes=8)
        self.assertEqual([row.path for row in snapshot.files], ["src/small.py"])
        self.assertEqual(len(snapshot.warnings), 1)
        self.assertIn("large.py", snapshot.warnings[0])

    def test_hydradb_preview_is_read_only(self):
        preview = render_hydradb_seed(self.root)
        self.assertEqual(preview["counts"]["objects"], 2)
        self.assertTrue(preview["statements"])
        self.assertFalse((self.root / "runtime").exists())

    def test_hydradb_seed_is_rebuildable_and_never_canonical(self):
        before = (self.root / "global" / "00_GLOBAL" / "global-object-registry.jsonl").read_bytes()
        report = build_hydradb_seed(self.root)
        target = Path(report.target)
        text = target.read_text(encoding="utf-8")
        self.assertEqual(report.status, "SUCCESS")
        self.assertTrue(str(target).startswith(str(self.root / "runtime")))
        self.assertIn("MERGE (p:AECProject", text)
        self.assertIn("MERGE (n:AECObject", text)
        self.assertIn("AEC_RELATION", text)
        self.assertIn("HOSTED_BY", text)
        self.assertNotIn("CREATE CONSTRAINT", text)
        self.assertEqual(report.counts["objects"], 2)
        self.assertEqual(before, (self.root / "global" / "00_GLOBAL" / "global-object-registry.jsonl").read_bytes())

    def test_hydradb_export_cannot_escape_runtime_directory(self):
        for target in [self.root / "global" / "bad.cypher", self.root / "outside.cypher"]:
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    build_hydradb_seed(self.root, target)

    def test_jevgrep_private_source_is_blocked_by_default(self):
        calls = []

        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        adapter = JevGrepAdapter(resolver=lambda _: "/usr/bin/jg", runner=runner)
        report = adapter.search("Where is provenance validated?", self.root)
        self.assertEqual(report.status, "REQUIRES_EGRESS_APPROVAL")
        self.assertEqual(calls, [])

    def test_jevgrep_can_run_after_explicit_approval(self):
        seen = {}

        def runner(command, **kwargs):
            seen["command"] = command
            seen["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="src/a.py:1", stderr="")

        adapter = JevGrepAdapter(resolver=lambda _: "/usr/bin/jg", runner=runner)
        report = adapter.search(
            "Where is provenance validated?",
            self.root,
            excludes=("runtime/",),
            allow_source_egress=True,
        )
        self.assertEqual(report.status, "SUCCESS")
        self.assertIn("--exclude", seen["command"])
        self.assertEqual(report.stdout, "src/a.py:1")
        self.assertFalse(seen["kwargs"]["check"])

    def test_jevgrep_files_is_available_without_egress_flag(self):
        adapter = JevGrepAdapter(
            resolver=lambda _: "/usr/bin/jg",
            runner=lambda command, **kwargs: SimpleNamespace(returncode=0, stdout="src 10\n", stderr=""),
        )
        report = adapter.files(self.root)
        self.assertEqual(report.status, "SUCCESS")
        self.assertIn("files", report.command)

    def test_graph_backend_selection_is_capability_driven(self):
        self.assertEqual(choose_graph_backend(GraphRequirements())["backend"], "Apache AGE")
        self.assertEqual(choose_graph_backend(GraphRequirements(local_only=False, object_store_durability=True))["backend"], "HydraDB")
        self.assertEqual(choose_graph_backend(GraphRequirements(requires_sparql=True))["backend"], "PyOxigraph")
        with self.assertRaises(ValueError):
            GraphRequirements(local_only=True, distributed_compute=True)

    def test_plan_never_promotes_accelerator_to_canonical(self):
        plan = intelligence_plan(private_source=True)
        self.assertTrue(all(stage.get("writes_source") is False for stage in plan["stages"]))
        graph = next(stage for stage in plan["stages"] if stage["stage"] == "graph-acceleration")
        self.assertFalse(graph["canonical"])
        jev = next(stage for stage in plan["stages"] if stage["stage"] == "context-selection")
        self.assertEqual(jev["source_egress"], "explicit-approval")

    def test_hydradb_config_contract(self):
        cfg = HydraDBConfig("http://127.0.0.1:8443", "token-32-bytes", graph_id="aec")
        self.assertTrue(cfg.query_url.endswith("/v1/graphs/aec/query"))
        with self.assertRaises(ValueError):
            HydraDBConfig("127.0.0.1:8443", "token")


if __name__ == "__main__":
    unittest.main()
