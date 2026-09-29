"""Rebuildable SQLite reference projection, separate from Ontology canonical stores.

Not the production vector/graph database. Use this to exercise contracts locally.
An authenticated gateway must derive allowed_source_ids, never trust client ACLs.
"""
import json
from pathlib import Path
import re
import sqlite3
from contextlib import contextmanager

from .contracts import SourceRevision, digest


SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, bundle_hash TEXT NOT NULL, bundle TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS active_sources (
  source_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, visible INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  project_id TEXT NOT NULL, kind TEXT NOT NULL, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS record_scope ON records(source_id, revision_id);
CREATE VIRTUAL TABLE IF NOT EXISTS text_fts USING fts5(id UNINDEXED, content, tokenize='unicode61');
"""


class Catalog:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.parent.is_dir():
            raise ValueError("Create a dedicated derived output directory first")
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def ingest(self, bundle, *, expected_current=None):
        source = SourceRevision(**bundle["source"])
        payload = json.dumps(bundle, ensure_ascii=False, sort_keys=True, allow_nan=False)
        checksum = digest(bundle)
        if any(r["locator"]["revision_id"] != source.revision_id or
               r["locator"]["source_id"] != source.source_id or
               r["source"] != source.to_dict() for r in bundle["records"]):
            raise ValueError("Bundle record identity mismatch")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM active_sources WHERE source_id=?", (source.source_id,)).fetchone()
            old = conn.execute("SELECT bundle_hash FROM revisions WHERE id=?", (source.revision_id,)).fetchone()
            if old and old["bundle_hash"] != checksum:
                raise ValueError("Immutable revision conflict: bump parser version after parser changes")
            if current and current["revision_id"] != source.revision_id and current["revision_id"] != expected_current:
                raise ValueError("Current revision changed; explicit compare-and-swap required")
            if current and not current["visible"]:
                raise ValueError("Revoked source cannot be reactivated by ingestion")
            if not old:
                conn.execute("INSERT INTO revisions VALUES(?,?,?,?)", (source.revision_id, source.source_id, checksum, payload))
                for r in bundle["records"]:
                    conn.execute("INSERT INTO records VALUES(?,?,?,?,?,?)", (
                        r["id"], source.source_id, source.revision_id, source.project_id,
                        r["kind"], json.dumps(r, ensure_ascii=False, allow_nan=False)))
                    conn.execute("INSERT INTO text_fts VALUES(?,?)", (r["id"], r["text"]))
            conn.execute("INSERT INTO active_sources VALUES(?,?,1) ON CONFLICT(source_id) "
                         "DO UPDATE SET revision_id=excluded.revision_id",
                         (source.source_id, source.revision_id))
        return source.revision_id

    def revoke(self, source_id):
        with self.connect() as conn:
            conn.execute("UPDATE active_sources SET visible=0 WHERE source_id=?", (source_id,))

    def get(self, record_id, *, allowed_source_ids):
        with self.connect() as conn:
            row = conn.execute("SELECT r.record,r.source_id FROM records r JOIN active_sources a "
                               "ON r.source_id=a.source_id AND r.revision_id=a.revision_id "
                               "WHERE r.id=? AND a.visible=1", (record_id,)).fetchone()
            if not row or row["source_id"] not in allowed_source_ids:
                return None
            return json.loads(row["record"])

    def search(self, query, *, allowed_source_ids, project_id=None, limit=20):
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        tokens = re.findall(r"[\w]+", query, flags=re.UNICODE)[:32]
        scopes = sorted(set(allowed_source_ids))
        if not tokens or not scopes:
            return []
        if len(scopes) > 500:
            raise ValueError("Reference catalog supports 500 scopes; production uses SQL ACL joins")
        # Quoted FTS terms prevent user text from becoming FTS query syntax.
        match = " OR ".join('"' + token + '"' for token in tokens)
        scope_sql = ",".join("?" for _ in scopes)
        project_sql = " AND r.project_id=?" if project_id else ""
        params = [match, *scopes, *([project_id] if project_id else []), limit]
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT r.record,bm25(text_fts) AS rank FROM text_fts "
                "JOIN records r ON r.id=text_fts.id JOIN active_sources a "
                "ON r.source_id=a.source_id AND r.revision_id=a.revision_id "
                f"WHERE text_fts MATCH ? AND a.visible=1 AND r.source_id IN ({scope_sql})"
                + project_sql + " ORDER BY rank,r.id LIMIT ?", params).fetchall()
        return [{"record": json.loads(row["record"]), "lexical_rank": row["rank"]} for row in rows]

    def export_current(self, *, allowed_source_ids):
        """Recheck authoritative membership immediately before projection export."""
        with self.connect() as conn:
            rows = conn.execute("SELECT r.record,r.source_id FROM records r JOIN active_sources a "
                                "ON r.source_id=a.source_id AND r.revision_id=a.revision_id "
                                "WHERE a.visible=1 ORDER BY r.id").fetchall()
        return [json.loads(r["record"]) for r in rows if r["source_id"] in allowed_source_ids]
