#!/usr/bin/env python3
"""Read-only acceptance harness for Ontology source -> live CAD identity mapping."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "extensions" / "drawing_context"))

from context_fabric.contracts import SourceRevision
from context_fabric.source_mapping import (
    LiveObjectObservation,
    TrustedSourceResolution,
    verify_source_binding,
)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="SourceRevision JSON")
    parser.add_argument("--record", type=Path, required=True, help="One drawing-context record JSON")
    parser.add_argument("--resolution", type=Path, required=True, help="TrustedSourceResolution JSON")
    parser.add_argument("--live", type=Path, required=True, help="LiveObjectObservation JSON")
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--require-review-ready",
        action="store_true",
        help="Also require the existing live-candidate guard to be VERIFIED_FOR_REVIEW.",
    )
    args = parser.parse_args()

    source = SourceRevision(**load(args.source))
    record = load(args.record)
    resolution = TrustedSourceResolution(**load(args.resolution))
    live = LiveObjectObservation(**load(args.live))
    report = verify_source_binding(record, source, resolution, live)

    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")

    if report["binding_state"] != "SOURCE_BOUND":
        return 2
    if args.require_review_ready and report["review_guard"]["status"] != "VERIFIED_FOR_REVIEW":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
