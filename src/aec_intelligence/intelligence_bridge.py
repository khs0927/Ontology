"""Bridge the packaged MCP gateway to the opt-in intelligence extension.

The extension stays outside the canonical aec_intelligence package so Jev and
experimental graph backends remain replaceable. This bridge loads it from the
current repository checkout and enforces repository/runtime boundaries.
"""

from __future__ import annotations

import importlib.util
import hashlib
from pathlib import Path
import sys
from typing import Any


def _load_extension(repository_root: str | Path):
    root = Path(repository_root).resolve()
    package_dir = root / "extensions" / "intelligence_fabric" / "intelligence_fabric"
    init_file = package_dir / "__init__.py"
    if not init_file.is_file():
        raise FileNotFoundError(f"intelligence_fabric extension not found: {init_file}")

    alias = "_aec_ontology_intelligence_fabric_" + hashlib.sha256(str(package_dir).encode("utf-8")).hexdigest()[:12]
    cached = sys.modules.get(alias)
    if cached is not None:
        return cached

    spec = importlib.util.spec_from_file_location(
        alias,
        init_file,
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load intelligence_fabric extension")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def _scoped_root(repository_root: str | Path, requested: str | Path | None) -> Path:
    root = Path(repository_root).resolve()
    candidate = root if requested in (None, "", ".") else (root / str(requested)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("context root must remain inside the repository") from exc
    if not candidate.is_dir():
        raise ValueError(f"context root is not a directory: {candidate}")
    return candidate


def inspect_code_context(repository_root: str | Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Use jevgrep only as a read-only context selector.

    Source egress is denied by default. The caller must explicitly opt in and
    can narrow the root and exclusion list before any external provider call.
    """
    extension = _load_extension(repository_root)
    question = str(arguments["question"])
    root = _scoped_root(repository_root, arguments.get("root"))
    command_prefix = tuple(arguments.get("command_prefix") or ["jg"])
    if command_prefix not in {("jg",), ("wsl", "jg")}:
        raise ValueError("command_prefix must be [\\"jg\\"] or [\\"wsl\\", \\"jg\\"]")
    excludes = tuple(arguments.get("excludes") or [
        ".git/",
        "runtime/",
        "projects/",
        "global/",
        "*.sqlite3",
        "*.db",
    ])
    report = extension.JevGrepAdapter(command_prefix=command_prefix).search(
        question,
        root,
        excludes=excludes,
        allow_source_egress=bool(arguments.get("allow_source_egress", False)),
        timeout=float(arguments.get("timeout", 60)),
    )
    return {
        **report.to_dict(),
        "mode": "READ_ONLY_CONTEXT_SELECTION",
        "canonical_mutation": False,
        "execution_authorized": False,
    }


def graph_backend_plan(repository_root: str | Path, arguments: dict[str, Any]) -> dict[str, Any]:
    extension = _load_extension(repository_root)
    requirements = extension.GraphRequirements(
        local_only=bool(arguments.get("local_only", True)),
        requires_sparql=bool(arguments.get("requires_sparql", False)),
        object_store_durability=bool(arguments.get("object_store_durability", False)),
        distributed_compute=bool(arguments.get("distributed_compute", False)),
        neo4j_protocol=bool(arguments.get("neo4j_protocol", False)),
    )
    result = extension.choose_graph_backend(requirements)
    return {
        "status": "SUCCESS",
        "requirements": {
            "local_only": requirements.local_only,
            "requires_sparql": requirements.requires_sparql,
            "object_store_durability": requirements.object_store_durability,
            "distributed_compute": requirements.distributed_compute,
            "neo4j_protocol": requirements.neo4j_protocol,
        },
        **result,
    }


def preview_hydradb_projection(repository_root: str | Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Render a HydraDB projection entirely in memory."""
    extension = _load_extension(repository_root)
    preview = extension.render_hydradb_seed(repository_root)
    limit = int(arguments.get("max_statements", 20))
    if not 0 <= limit <= 200:
        raise ValueError("max_statements must be between 0 and 200")
    statements = preview["statements"]
    return {
        "status": "SUCCESS",
        "backend": "HydraDB",
        "canonical_mutation": False,
        "statement_count": len(statements),
        "counts": preview["counts"],
        "statements": statements[:limit],
        "truncated": len(statements) > limit,
    }


def export_hydradb_projection(repository_root: str | Path) -> dict[str, Any]:
    """Write only the rebuildable runtime seed under runtime/hydradb/."""
    root = Path(repository_root).resolve()
    extension = _load_extension(root)
    report = extension.build_hydradb_seed(root)
    target = Path(report.target).resolve()
    runtime_root = (root / "runtime").resolve()
    try:
        target.relative_to(runtime_root)
    except ValueError as exc:
        raise ValueError("HydraDB export escaped the runtime directory") from exc
    return {
        **report.to_dict(),
        "canonical_mutation": False,
        "rebuildable": True,
    }
