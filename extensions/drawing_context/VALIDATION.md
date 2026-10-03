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


## RAGFlow HTTP contract — 2026-10-01

A disabled-by-default HTTP sidecar contract now targets the reviewed RAGFlow
`v0.27.2` API profile. It requires a pinned release image digest before enablement,
requires HTTPS for non-loopback servers, and keeps the bearer key outside canonical
data.

Remote RAGFlow chunk IDs are never treated as canonical identifiers. They are rebound
through a local, rebuildable mapping registry to drawing-context projection IDs and
their canonical/source/revision/hash metadata.

Two retrieval paths are intentionally separate:

- `search()`: production-safe. It requires allowed source IDs and current revision
  IDs and returns only mapped, authorized, current-revision hits.
- `benchmark_search()`: diagnostic-only. It can expose unmapped/stale/unauthorized
  remote results so the benchmark can measure leakage; it must not be used as user
  context.

Fake-transport tests cover chunk creation/binding, safe retrieval, unmapped result
handling, revision replacement ordering, source purge, partial-upload rollback and
configuration fail-closed behavior.

Still **not validated**: a real RAGFlow server, real v0.27.2 image digest, real API key,
network failure recovery under production load, parser/index completion, or retrieval
quality on the user's Drive corpus. Those remain G4 acceptance work.


## RAGFlow deployment preparation — 2026-10-01

The repository now includes a read-only preflight for the official upstream
`v0.27.2` deployment path, runtime-only atomic binding persistence, and a
read-only live benchmark runner.

The preflight observes Docker/Compose versions, CPU/RAM/free disk, architecture,
`vm.max_map_count` when visible, and an already-pulled image RepoDigest. It
prints reviewed upstream commands but does not clone, pull, start, stop, or
delete containers.

The deployment profile follows the reviewed official minimums: CPU >= 4 cores,
RAM >= 16 GB, disk >= 50 GB, Docker >= 24.0.0, Compose >= 2.26.1 and
`vm.max_map_count >= 262144` when Elasticsearch requires it. The prebuilt
image path is treated as x86_64/amd64.

Bindings may persist only under `runtime/ragflow/`; loading a missing registry
is side-effect free, while saving uses temp-file + atomic replace. The live
benchmark runner performs retrieval only and reuses the existing provenance,
authorization, revision, Recall@5 and MRR promotion gates.

No real RAGFlow instance was started by these changes.


## LightRAG comparison contract — 2026-10-02

A disabled-by-default LightRAG comparison adapter now targets the reviewed
`v1.5.7` API profile and uses `POST /query/data` so retrieval can be
measured without treating generated answers as benchmark evidence.

The adapter requires an API key, validates credentials through
`GET /auth/verify` before the first query, requires HTTPS for non-loopback
servers, rejects credentials embedded in URLs, and requires a pinned SHA-256
container image digest before enablement.

Remote `chunk_id` values are never canonical. A local derived binding under
`runtime/lightrag/` must map the chunk to an existing drawing-context
projection and its canonical/source/revision/hash metadata. Production search
drops unmapped, unauthorized and stale-revision chunks before returning their
content. Diagnostic benchmark search may surface an unmapped hit only with an
empty metadata object so the shared benchmark gate records provenance failure.

The same provider-neutral hard gates used for RAGFlow apply to LightRAG:
100% provenance metadata coverage, zero unauthorized leakage, zero stale
revision leakage, Recall@5 >= 0.80 and MRR >= 0.60.

Still **not validated**: a live LightRAG v1.5.7 instance, real credentials,
document ingestion/index completion, automatic remote chunk binding, or
retrieval quality on the user's Drive corpus. This is comparison preparation,
not a production adoption claim.
