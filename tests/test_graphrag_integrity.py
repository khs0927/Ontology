"""Offline regressions for derived-data invalidation and partial legal evidence."""

import copy
import hashlib
from contextlib import nullcontext

import pytest

from aec_intelligence.operational.graphrag import kg
from aec_intelligence.operational.graphrag.integrations import project_facts, subject_ref


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class DB:
    def __init__(self, conn):
        self.conn = conn

    def connect(self, **kwargs):
        return nullcontext(self.conn)


@pytest.mark.parametrize("field,value", [
    ("unit_weight_kg_m", 10.0), ("dims_mm", [100, 50, 6, 7]), ("source_sha256", "b" * 64),
])
def test_catalog_value_change_rebuilds_unchanged_project(monkeypatch, field, value):
    """A same-key correction must invalidate the already-built project, then settle on a new cache key."""
    state = {}
    writes = []

    class Conn:
        def execute(self, sql):
            return Result([{"project_key": key, "fingerprint": fp} for key, fp in state.items()])

    builder = kg.KnowledgeGraphBuilder(DB(Conn()))
    builder.catalog = {"H-100x50x5x7": {"unit_weight_kg_m": 9.3, "dims_mm": [100, 50, 5, 7],
                                         "source_sha256": "a" * 64}}
    docs = [{"id": "d", "revision": 0, "updated_at": "2026-10-04"}]
    monkeypatch.setattr(kg, "load_documents", lambda conn: {"p": docs})
    graph = kg.ProjectGraph("p")
    graph.node("kg:p:p", "Project", "P")
    monkeypatch.setattr(builder, "project_graph", lambda *args: graph)

    def save(conn, graph, fp):
        state[graph.key] = fp
        writes.append(fp)

    monkeypatch.setattr(builder, "_write", save)
    assert builder.build()["projects"] == 1
    assert builder.build()["skipped"] == 1
    builder.catalog["H-100x50x5x7"][field] = value
    assert builder.build()["projects"] == 1
    assert builder.build()["skipped"] == 1
    assert len(writes) == 2 and writes[0] != writes[1]


def test_legacy_catalog_retains_byte_provenance(tmp_path):
    raw = b"Spec Shape M2 M3 M4 M5 M6 M7 Weight Paint Color\nH100x50x5x7 H 100 50 5 7 8 0 9.3 0.4 7\n"
    (tmp_path / "H-BEAM.dat").write_bytes(raw)
    assert kg.load_steel_catalog(tmp_path)["H-100x50x5x7"]["source_sha256"] == hashlib.sha256(raw).hexdigest()


def test_document_byte_hash_and_parser_revision_invalidate_fingerprint():
    doc = {"id": "d", "revision": 0, "updated_at": "same", "source_hash": "a" * 64}
    original = kg.fingerprint([doc])
    assert kg.fingerprint([dict(doc, source_hash="b" * 64)]) != original
    assert kg.fingerprint([dict(doc, revision=1)]) != original


@pytest.mark.parametrize("change,stale", [(None, False), ("revision", True), ("source_hash", True), ("unknown", True)])
def test_rule_evaluation_invalidated_by_parser_revision(change, stale):
    old = {"id": "d", "project_id": "P", "revision": 0, "source_hash": "a" * 64}
    ref = subject_ref("P", document=old, locator={"kg_node": "kg:d:d"})
    current = dict(old)
    if change == "revision":
        current["revision"] = 1  # same bytes, different parser output
    elif change == "source_hash":
        current["source_hash"] = "b" * 64
    graph = kg.ProjectGraph("p")
    graph.node("kg:p:p", "Project", "P")
    drawing = graph.node("kg:d:d", "Drawing", "D")
    drawing.document_ids = [] if change == "unknown" else ["d"]
    builder = kg.KnowledgeGraphBuilder(DB(None))
    builder.requirements = [{"id": "synthetic-test", "title": "Synthetic test rule", "space_uses": [],
                             "subjects": [ref], "outcome": "PASS"}]
    docs = [dict(current, id="other", source_hash="b" * 64)] if change == "unknown" else [current]
    builder._requirements(None, graph, "p", docs)
    evidence = next(iter(graph.edges.values()))
    assert evidence["stale"] is stale
    requirement = next(n for n in graph.nodes.values() if n.type == "Requirement")
    assert requirement.props["outcome"] == ("REVIEW" if stale else "PASS")
    assert requirement.props["exported_outcome"] == "PASS"


def test_later_fresh_ref_cannot_hide_stale_parser_ref_for_same_node():
    old = {"id": "d", "project_id": "P", "revision": 0, "source_hash": "a" * 64}
    current = dict(old, revision=1)
    refs = [subject_ref("P", document=d, locator={"kg_node": "kg:d:d"}) for d in (old, current)]
    graph = kg.ProjectGraph("p")
    graph.node("kg:p:p", "Project", "P")
    graph.node("kg:d:d", "Drawing", "D").document_ids = ["d"]
    builder = kg.KnowledgeGraphBuilder(DB(None))
    builder.requirements = [{"id": "synthetic-test", "title": "Synthetic test rule", "space_uses": [],
                             "subjects": refs, "outcome": "PASS"}]
    builder._requirements(None, graph, "p", [current])
    assert next(iter(graph.edges.values()))["stale"]
    assert next(n for n in graph.nodes.values() if n.type == "Requirement").props["outcome"] == "REVIEW"


def test_subject_locator_cannot_grant_cross_project_scope():
    doc = {"id": "d", "project_id": "P", "revision": 0, "source_hash": "a" * 64}
    ref = subject_ref("another-project", document=doc, locator={"kg_node": "kg:d:d"})
    graph = kg.ProjectGraph("p")
    graph.node("kg:p:p", "Project", "P")
    graph.node("kg:d:d", "Drawing", "D").document_ids = ["d"]
    builder = kg.KnowledgeGraphBuilder(DB(None))
    builder.requirements = [{"id": "synthetic-test", "title": "Synthetic test rule", "space_uses": [],
                             "subjects": [ref], "outcome": "PASS"}]
    builder._requirements(None, graph, "p", [doc])
    assert not graph.edges and not any(n.type == "Requirement" for n in graph.nodes.values())


def test_partial_floor_labels_do_not_export_total_building_counts():
    proj = {"id": "kg:p:p", "name": "P", "props": {"source_project_ids": ["P"]}}
    nodes = [{"id": "floor", "type": "Storey", "name": "3F", "props": {}, "document_ids": ["d"],
              "object_ids": []},
             {"id": "basement", "type": "Storey", "name": "B2", "props": {}, "document_ids": ["d"],
              "object_ids": []}]

    class Conn:
        def execute(self, sql, params):
            if "type='Project'" in sql:
                return Result([copy.deepcopy(proj)])
            if "FROM aec.kg_nodes" in sql:
                return Result(copy.deepcopy(nodes))
            return Result([])

    exported = project_facts(DB(Conn()), "p")
    assert exported["facts"]["building"] == {
        "highest_observed_floor": 3, "deepest_observed_basement": 2, "storeys": ["B2", "3F"]}
    assert {"building.floor_count", "building.basement_count"} <= set(exported["absent_by_design"])
    assert exported["provenance"]["building.highest_observed_floor"]["authoritative_total"] is False
    assert exported["provenance"]["building.storeys"]["coverage"] == "partial"
