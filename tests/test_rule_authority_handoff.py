"""Synthetic contract fixtures exercise authority gates; these are not actual legal evidence."""

import copy
import json
from datetime import date

import pytest

from aec_intelligence.operational.graphrag.integrations import canonical_context_problems, load_rules
from aec_intelligence.operational.graphrag.kg import KnowledgeGraphBuilder, ProjectGraph


def context():
    return {"schema": "archontos-canonical-context/1", "rule_version_id": "00000000-0000-0000-0000-000000000001",
            "evaluation_id": "00000000-0000-0000-0000-000000000002",
            "source_version_id": "00000000-0000-0000-0000-000000000003",
            "assertion_ids": ["00000000-0000-0000-0000-000000000004"], "project_key": "p",
            "review_status": "approved", "status": "active", "binding": True, "synthetic": False,
            "valid_from": "2020-01-01", "valid_to": "2099-12-31", "evaluated_at": "2026-01-01T12:00:00+09:00"}


@pytest.mark.parametrize("field,value", [
    ("schema", "other/1"), ("review_status", "unreviewed"), ("status", "suspended"), ("status", "retired"),
    ("binding", False), ("synthetic", True), ("synthetic", None), ("assertion_ids", []),
    ("assertion_ids", ["bad"]), ("evaluation_id", "bad"), ("rule_version_id", None), ("source_version_id", 42),
    ("project_key", ""), ("valid_from", "2027-01-01"), ("valid_to", "2025-12-31"),
    ("valid_from", "invalid"), ("evaluated_at", "2026-01-01"), ("evaluated_at", "2027-01-01T00:00:00Z"),
])
def test_canonical_context_rejects_unsafe_or_incomplete_authority(field, value):
    item = context()
    item[field] = value
    assert canonical_context_problems(item, today=date(2026, 10, 4))


def test_effective_interval_checks_evaluation_and_current_dates_inclusive():
    item = context()
    item.update(valid_from="2026-10-04", valid_to="2026-10-04", evaluated_at="2026-10-04T00:00:00Z")
    assert canonical_context_problems(item, today=date(2026, 10, 4)) == []
    assert canonical_context_problems(item, today=date(2026, 10, 5))
    item["evaluated_at"] = "2026-10-03T12:00:00Z"
    assert canonical_context_problems(item, today=date(2026, 10, 4))


@pytest.mark.parametrize("metadata", [None, {}, "invalid", dict(context(), synthetic=True)])
def test_ungoverned_export_is_review_without_losing_original_branch(tmp_path, metadata):
    row = {"rule_id": "synthetic", "outcome": "PASS", "canonical_context": metadata,
           "applies_to": [{"project_id": "P"}]}
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"schema": "archontos-rule-export/1", "rules": [row]}), encoding="utf-8")
    rules, problems = load_rules(path)
    assert rules[0]["outcome"] == "REVIEW" and rules[0]["exported_outcome"] == "PASS"
    assert rules[0]["authority_problems"] and any("requires REVIEW" in p for p in problems)


def test_valid_contract_cannot_certify_invalid_subject_or_other_project(tmp_path):
    row = {"rule_id": "synthetic", "outcome": "PASS", "canonical_context": context(),
           "applies_to": [{"project_id": "P"}, {"project_id": ""}]}
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"schema": "archontos-rule-export/1", "rules": [row]}), encoding="utf-8")
    rules, _ = load_rules(path)
    assert rules[0]["outcome"] == "REVIEW"
    row["applies_to"].pop()
    path.write_text(json.dumps({"schema": "archontos-rule-export/1", "rules": [row]}), encoding="utf-8")
    rules, problems = load_rules(path)
    assert rules[0]["outcome"] == "PASS" and not problems
    builder = KnowledgeGraphBuilder(None)
    builder.requirements = rules
    for project, expected in (("p", "PASS"), ("other", "REVIEW")):
        graph = ProjectGraph(project)
        graph.node(f"kg:p:{project}", "Project", "P")
        builder._requirements(None, graph, project, [{"id": "d", "project_id": "P", "source_hash": None}])
        node = next(n for n in graph.nodes.values() if n.type == "Requirement")
        assert node.props["outcome"] == expected
        assert node.props["exported_outcome"] == "PASS"
        assert bool(node.props["authority_problems"]) == (project == "other")


def test_malformed_rule_shapes_are_reported_without_linking(tmp_path):
    path = tmp_path / "rules.json"
    for rows in ("bad", [{"rule_id": "r", "applies_to": "P"}], [{"rule_id": "r", "space_uses": [1]}]):
        path.write_text(json.dumps({"schema": "archontos-rule-export/1", "rules": copy.deepcopy(rows)}), encoding="utf-8")
        rules, problems = load_rules(path)
        assert not rules and problems
