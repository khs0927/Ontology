#!/usr/bin/env python3
"""Compare provider-neutral RAG benchmark result wrappers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "extensions" / "drawing_context"))

from context_fabric.compare import (
    compare_provider_runs,
    load_provider_run,
    save_comparison,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare RAG provider benchmark runs from the exact same fixture."
    )
    parser.add_argument("runs", nargs="+", help="Provider-run JSON files (at least two)")
    parser.add_argument("--out", help="Optional comparison JSON output path")
    args = parser.parse_args()

    if len(args.runs) < 2:
        parser.error("at least two provider-run JSON files are required")

    runs = [load_provider_run(path) for path in args.runs]
    report = compare_provider_runs(runs)
    if args.out:
        save_comparison(report, args.out)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["status"] in {"SELECTED", "TIE"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
