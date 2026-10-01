# Ontology Intelligence Fabric (opt-in)

This extension adds a **Jev context-selection layer** and an **optional graph acceleration layer** without changing the repository's canonical CAIR/source model.

```text
Original artifacts
  -> existing parsers / CAIR
  -> canonical JSON/JSONL + provenance                (authoritative)
  -> intelligence_fabric                             (derived only)
       |- Jevgrep external CLI context selection      (explicit source-egress approval)
       |- Apache AGE / PyOxigraph                     (local-first baseline)
       `- HydraDB external HTTP/OpenCypher service    (distributed/object-store option)
```

## Why it is isolated

The existing repository already has the right boundary: raw artifacts and CAIR remain authoritative, while graph/vector/runtime stores are rebuildable. This extension keeps that rule. It does not rewrite CAD, source documents, CAIR snapshots, or files under `global/`.

## Jevgrep integration

`dzhng/jevgrep` is treated as an **external CLI**, not copied into this repository. Its upstream documentation states that eligible source content can be sent to the configured provider. Therefore searches are blocked by default:

```python
from intelligence_fabric import JevGrepAdapter

jev = JevGrepAdapter()
report = jev.search("Where is provenance checked?", ".")
assert report.status == "REQUIRES_EGRESS_APPROVAL"

# Only for a root approved to leave the machine/provider boundary:
report = jev.search(
    "Where is provenance checked?",
    ".",
    excludes=("runtime/", "projects/private/"),
    allow_source_egress=True,
)
```

`jev.files(root)` is available as a preflight inventory. On Windows, upstream jevgrep currently documents macOS/Linux support; use a WSL-visible checkout or a wrapper and configure `command_prefix` accordingly.

## HydraDB integration

HydraDB is used only as an **external graph service boundary**. No HydraDB AGPL source is vendored here. The extension can generate conservative OpenCypher from canonical global JSONL and can talk to an already-running HydraDB HTTP endpoint with Python's standard library.

```python
from intelligence_fabric import build_hydradb_seed, HydraDBConfig, HydraDBHTTPAdapter

export = build_hydradb_seed(".")
# -> runtime/hydradb/seed.cypher

client = HydraDBHTTPAdapter(
    HydraDBConfig(
        base_url="http://127.0.0.1:8443",
        token="...",
        namespace="default",
        graph_id="aec",
        cell_id="cell-0",
    )
)
# client.health()
# client.apply_seed(export.target)
```

The seed deliberately avoids Neo4j-only constraints and procedures. Ontology predicates remain relationship properties rather than executable relationship type strings.

## Capability-driven graph backend

```python
from intelligence_fabric import GraphRequirements, choose_graph_backend

choose_graph_backend(GraphRequirements())
# Apache AGE: local-first default already aligned with this repository

choose_graph_backend(GraphRequirements(local_only=False, object_store_durability=True))
# HydraDB: external distributed/object-store accelerator

choose_graph_backend(GraphRequirements(requires_sparql=True))
# PyOxigraph: local RDF/SPARQL semantic index
```

## Test

From this directory:

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

No network, provider key, HydraDB server, or Jev installation is required for the unit tests.


## Incremental code intelligence

The code-intelligence layer follows the same rule as CAIR: **source files are truth; code graphs and indexes are derived**. `scan_code_tree()` builds a deterministic SHA-256 inventory and `refresh_code_snapshot()` compares it with the previous runtime snapshot.

```python
from intelligence_fabric import refresh_code_snapshot

report = refresh_code_snapshot(".")
# runtime/code-intelligence/snapshot.json
# report["diff"]["derived_code_graph_stale"] == True when source changed
```

Snapshots are restricted to `runtime/code-intelligence/`. They never write `global/`, project CAIR, or source files. Each file record carries `provenance="filesystem"` and `evidence="EXTRACTED"`. Added, modified, and deleted paths are reported explicitly so a future Tree-sitter/Graphify-style code graph can be incrementally reconciled rather than treated as authoritative.
