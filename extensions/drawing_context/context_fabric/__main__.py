"""Offline CLI. Outputs go only to an explicitly separate derived directory."""
import argparse
import json
from pathlib import Path

from .adapters import adapt_snapshot, ragflow_projection
from .catalog import Catalog
from .contracts import SourceRevision, file_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import-snapshot")
    imp.add_argument("--snapshot", required=True, type=Path)
    imp.add_argument("--source", required=True, type=Path)
    imp.add_argument("--captured-file", required=True, type=Path)
    imp.add_argument("--out", required=True, type=Path)
    imp.add_argument("--expected-current")
    query = sub.add_parser("search")
    query.add_argument("--catalog", required=True, type=Path)
    query.add_argument("--query", required=True)
    query.add_argument("--allowed-source", action="append", required=True)
    query.add_argument("--project")
    args = parser.parse_args()
    if args.command == "import-snapshot":
        # Snapshot and canonical source folders may never also be output folders.
        out = args.out.resolve()
        for source_file in (args.snapshot, args.source, args.captured_file):
            input_path = source_file.resolve()
            if out == input_path.parent or out in input_path.parents or input_path.parent in out.parents:
                parser.error("Output must be a separate directory tree from all inputs")
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8-sig"))
        manifest = SourceRevision(**json.loads(args.source.read_text(encoding="utf-8-sig")))
        if file_hash(args.captured_file) != manifest.sha256:
            parser.error("Captured source bytes do not match manifest sha256")
        bundle = adapt_snapshot(snapshot, manifest)
        out.mkdir(parents=True, exist_ok=True)
        catalog = Catalog(out / "catalog.sqlite3")
        revision = catalog.ingest(bundle, expected_current=args.expected_current)
        # Catalog is the atomic artifact. Projections are generated on demand after ACL recheck.
        records = catalog.export_current(allowed_source_ids={manifest.source_id})
        print(json.dumps({"source_id": manifest.source_id, "revision_id": revision,
                          "records": len(records), "ragflow_projection": ragflow_projection(records)},
                         ensure_ascii=False, indent=2))
    else:
        if not args.catalog.is_file():
            parser.error("Catalog does not exist")
        print(json.dumps(Catalog(args.catalog).search(args.query,
                         allowed_source_ids=set(args.allowed_source), project_id=args.project),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
