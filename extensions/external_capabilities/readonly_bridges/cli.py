"""CLI for recording headless read-only bridge contract evidence.

This CLI is intentionally non-native: it consumes an already-produced projection
JSON and records only the readonly-bridge-contract capability in EvidenceHistory.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from capability_registry.history import EvidenceHistory
from .evidence import CONTRACT_SCOPE, record_readonly_probe_evidence


def build_parser():
    parser = argparse.ArgumentParser(prog="readonly-bridge-evidence")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="Append one headless bridge projection to EvidenceHistory")
    record.add_argument("--projection", required=True, help="Projection JSON path")
    record.add_argument("--ledger", required=True, help="SQLite EvidenceHistory path")
    record.add_argument("--id", required=True, help="Stable evidence identifier")
    record.add_argument("--run-url", required=True)
    record.add_argument("--run-timestamp", required=True)
    record.add_argument("--valid-until", required=True)

    show = sub.add_parser("show", help="Project one exact headless contract scope from the ledger")
    show.add_argument("--ledger", required=True)
    show.add_argument("--scope", required=True, help="Evidence-record JSON used as exact scope")
    show.add_argument("--now", default=None, help="Aware ISO timestamp; default current UTC")
    return parser


def _read_json(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON input must be an object")
    return data


def main(argv=None):
    args = build_parser().parse_args(argv)
    history = EvidenceHistory(args.ledger)
    try:
        if args.command == "record":
            projection = _read_json(args.projection)
            record = record_readonly_probe_evidence(
                history,
                args.id,
                projection,
                run_url=args.run_url,
                run_timestamp=args.run_timestamp,
                valid_until=args.valid_until,
            )
            result = {
                "recorded": True,
                "evidence_id": args.id,
                "capability_id": record["capability_id"],
                "outcome": record["outcome"],
                "verification_kind": record["verification_kind"],
                "contract_scope": CONTRACT_SCOPE,
                "execution_allowed": False,
                "canonical_allowed": False,
            }
        else:
            scope = _read_json(args.scope)
            now = datetime.now(timezone.utc) if args.now is None else datetime.fromisoformat(
                args.now.replace("Z", "+00:00")
            )
            result = history.project(scope=scope, now=now)
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        history.close()


if __name__ == "__main__":
    raise SystemExit(main())
