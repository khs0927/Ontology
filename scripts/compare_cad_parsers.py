#!/usr/bin/env python3
"""Compare two CAD parser census files without selecting a truth oracle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "extensions" / "drawing_context"))

from context_fabric.cad_discrepancy import (
    HandleMappingEvidence,
    compare_censuses,
    file_sha256,
    normalize_census,
)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def normalized(
    path: Path,
    *,
    provider: str,
    provider_version: str,
    source_sha256: str,
    handle_space: str,
    adapter_commit: str | None,
):
    payload = load(path)
    return normalize_census(
        payload,
        provider=provider,
        provider_version=provider_version,
        source_sha256=source_sha256,
        handle_space=handle_space,
        adapter_commit=adapter_commit,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Exact source DWG/DXF used by both lanes")
    parser.add_argument("--left", type=Path, required=True)
    parser.add_argument("--right", type=Path, required=True)
    parser.add_argument("--left-provider", required=True)
    parser.add_argument("--right-provider", required=True)
    parser.add_argument("--left-version", required=True)
    parser.add_argument("--right-version", required=True)
    parser.add_argument("--left-handle-space", default="unknown")
    parser.add_argument("--right-handle-space", default="unknown")
    parser.add_argument("--left-adapter-commit")
    parser.add_argument("--right-adapter-commit")
    parser.add_argument(
        "--handle-mapping",
        type=Path,
        help="Optional independently verified handle-mapping evidence JSON.",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    source_hash = file_sha256(args.source)
    left = normalized(
        args.left,
        provider=args.left_provider,
        provider_version=args.left_version,
        source_sha256=source_hash,
        handle_space=args.left_handle_space,
        adapter_commit=args.left_adapter_commit,
    )
    right = normalized(
        args.right,
        provider=args.right_provider,
        provider_version=args.right_version,
        source_sha256=source_hash,
        handle_space=args.right_handle_space,
        adapter_commit=args.right_adapter_commit,
    )

    mapping = None
    if args.handle_mapping:
        mapping = HandleMappingEvidence(**load(args.handle_mapping))

    report = compare_censuses(left, right, handle_mapping=mapping)
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 2 if report["status"] == "INCOMPARABLE" else 0


if __name__ == "__main__":
    raise SystemExit(main())
