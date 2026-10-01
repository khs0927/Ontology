"""Opt-in decision and graph acceleration for the AEC Ontology repository.

Canonical CAIR/JSON/JSONL files remain authoritative. This package only plans,
reads, exports, or talks to explicitly configured external accelerators.
"""

from .code import (
    CodeFileRecord,
    CodeSnapshot,
    diff_code_snapshots,
    load_code_snapshot,
    refresh_code_snapshot,
    save_code_snapshot,
    scan_code_tree,
)
from .graph import HydraDBConfig, HydraDBHTTPAdapter, build_hydradb_seed, render_hydradb_seed
from .jev import JevGrepAdapter, JevGrepReport
from .planning import GraphRequirements, choose_graph_backend, intelligence_plan

__all__ = [
    "CodeFileRecord",
    "CodeSnapshot",
    "GraphRequirements",
    "HydraDBConfig",
    "HydraDBHTTPAdapter",
    "JevGrepAdapter",
    "JevGrepReport",
    "build_hydradb_seed",
    "render_hydradb_seed",
    "choose_graph_backend",
    "diff_code_snapshots",
    "load_code_snapshot",
    "refresh_code_snapshot",
    "save_code_snapshot",
    "scan_code_tree",
    "intelligence_plan",
]

__version__ = "0.1.0"
