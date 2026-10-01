# Validation record — 2026-09-29

Baseline Ontology commit: `d4d1742bf7395c7c97dae6479bf1456524ceaaec`.
Baseline Power CAD commit: `4ac21db718417cbf69e35f66979395e031853623`.

Only `extensions/drawing_context/` is added to Ontology. No existing parser,
CAIR schema, dependency declaration, service configuration or source file changes.

Command, from Ontology root:

```bash
PYTHONPATH=src:extensions/drawing_context python -m unittest discover -s extensions/drawing_context/tests -v
```

Result: **21 tests passed, zero skipped**, Python 3.12, Linux, ezdxf available.
The integration test generates a real DXF with two layouts and Korean text,
calls the existing operational parser, preserves the original checksum and
retrieves a record with its source handle. Other cases cover source scoping,
immutable revision conflict, compare-and-swap, revocation, ACL filtering before
limits, malformed FTS input, unknown CAD locations, embedding space mismatch,
CAIR compatibility and native handoff guards. No external service was mocked
and then reported as a real deployment.

Not validated: real Drive credentials/downloads/change replay, licensed DWG
conversion, production PostgreSQL/graph/vector services, RAGFlow HTTP integration,
AutoCAD 2027 loading or object resolution, C++ builds, throughput on the user's
corpus. These remain the explicit G1–G5 acceptance gates in FRAMEWORK.ko.md.

The local catalog and planner are reference components. The planner does not
execute jobs. The handoff guard checks caller-supplied observations, does not
contact AutoCAD and does not authorize editing.


## Retrieval benchmark gate — 2026-10-01

A provider-neutral benchmark harness now exists in `context_fabric/benchmark.py`.
The first target is RAGFlow because the repository already has a provenance-preserving
RAGFlow projection DTO and Drive-oriented document flow. This is **not** a claim that
RAGFlow HTTP integration or production retrieval has been validated.

Promotion hard gates:
- provenance metadata coverage = 100%
- unauthorized source leakage = 0
- stale revision leakage = 0
- Recall@5 >= 0.80
- MRR >= 0.60

p50/p95 latency, indexing time and storage size are recorded during the first live
benchmark phase but are not initial hard gates. The benchmark result and fixture both
declare `canonical_mutation=false`; provider results remain derived search candidates.
