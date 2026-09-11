"""Rebuildable runtime registry; canonical data remains in portable files."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from typing import Any

from .storage import ArtifactRecord


class ArtifactRegistry:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                filename TEXT NOT NULL,
                local_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL,
                source_artifact_id TEXT,
                storage_provider TEXT NOT NULL,
                storage_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                security_classification TEXT NOT NULL DEFAULT 'INTERNAL',
                UNIQUE(project_id, artifact_type, sha256)
            );
            CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_hash ON artifacts(sha256);
            CREATE TABLE IF NOT EXISTS objects (
                object_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                object_type TEXT NOT NULL,
                classification_json TEXT,
                geometry_ref TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_objects_project ON objects(project_id);
            """
        )
        try:
            self.connection.execute("ALTER TABLE artifacts ADD COLUMN security_classification TEXT NOT NULL DEFAULT 'INTERNAL'")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        self.connection.commit()

    def register_project(self, project_id: str, name: str, created_at: str, status: str = "ACTIVE", **metadata: Any) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO projects(project_id,name,status,created_at,metadata_json) VALUES (?,?,?,?,?)",
            (project_id, name, status, created_at, json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
        )
        self.connection.commit()

    def register_artifact(self, record: ArtifactRecord) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO artifacts
            (artifact_id,project_id,artifact_type,filename,local_path,sha256,size,created_at,status,version,
             source_artifact_id,storage_provider,storage_id,metadata_json,security_classification)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.artifact_id,
                record.project_id,
                record.artifact_type,
                record.filename,
                record.local_path,
                record.sha256,
                record.size,
                record.created_at,
                record.status,
                record.version,
                record.source_artifact_id,
                record.storage_provider,
                record.storage_id,
                json.dumps(record.metadata, ensure_ascii=False, sort_keys=True),
                record.security_classification,
            ),
        )
        self.connection.commit()

    def list_artifacts(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id is None:
            rows = self.connection.execute("SELECT * FROM artifacts ORDER BY created_at, artifact_id").fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM artifacts WHERE project_id=? ORDER BY created_at, artifact_id", (project_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def register_object(self, object_id: str, project_id: str, object_type: str, classification: dict[str, Any] | None, geometry_ref: str | None) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO objects(object_id,project_id,object_type,classification_json,geometry_ref) VALUES (?,?,?,?,?)",
            (object_id, project_id, object_type, json.dumps(classification, ensure_ascii=False, sort_keys=True) if classification else None, geometry_ref),
        )
        self.connection.commit()

    def list_objects(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id is None:
            rows = self.connection.execute("SELECT * FROM objects ORDER BY object_id").fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM objects WHERE project_id=? ORDER BY object_id", (project_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["classification"] = json.loads(item.pop("classification_json")) if item.get("classification_json") else None
            item["id"] = item.pop("object_id")
            item["type"] = item.pop("object_type")
            result.append(item)
        return result

    def close(self) -> None:
        self.connection.close()
