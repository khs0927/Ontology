"""FastAPI REST API server for AEC intelligence ingestion, search, object inspection, and review."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from ..classifier import normalize_storey
from . import catalog
from .auth import BearerTokenMiddleware, api_token_from_env
from .config import Settings
from .db import Database
from .ingest_jobs import ingest_job
from .parsers import SUPPORTED
from .search import SearchRouter


class IngestRequest(BaseModel):
    path: str = Field(..., description="Absolute path to file or directory")
    project_id: str = Field(default="P-DEFAULT", description="Project identifier")
    discipline: str = Field(default="ARCH", description="Discipline code (e.g. ARCH, STRUCT, MEP)")
    # Only these two queues have workers; any other value would leave the job QUEUED forever.
    queue: Literal["cad", "ocr"] = Field(default="cad", description="Target worker queue (cad or ocr)")


class SearchRequest(BaseModel):
    query: str = Field(..., description="Korean or English architectural search query")
    project_id: str | None = Field(default=None, description="Optional project filter")
    discipline: str | None = Field(default=None, description="Optional discipline filter")
    storey: str | None = Field(default=None, description="Optional storey/floor filter")
    revision: int | None = Field(default=None, description="Optional revision filter")
    kind: str | None = Field(default=None, description="Optional object kind (e.g. Wall, Door, Window)")
    top_k: int = Field(default=10, ge=1, le=100, description="Max results to return")
    expand_graph: bool = Field(default=True, description="Whether to expand relations via Apache AGE")


class ReviewAction(BaseModel):
    object_id: str = Field(..., description="ID of the candidate object")
    action: Literal["CONFIRM", "REJECT", "CHANGE_TYPE"] = Field(..., description="CONFIRM, REJECT, or CHANGE_TYPE")
    new_type: str | None = Field(default=None, description="New architectural type if action is CHANGE_TYPE")
    notes: str | None = Field(default=None, description="Review notes / explanation")

    @model_validator(mode="after")
    def _change_type_needs_type(self) -> "ReviewAction":
        # Without this, CHANGE_TYPE with no new_type silently became a CONFIRM and still wrote a revision.
        if self.action == "CHANGE_TYPE" and not (self.new_type or "").strip():
            raise ValueError("new_type is required when action is CHANGE_TYPE")
        return self


def cors_origins_from_env(value: str | None = None) -> list[str]:
    """Explicit cross-origin allow-list from ``AEC_CORS_ORIGINS`` (comma separated).

    The bundled dashboard is served from the API's own origin and power-cad-mcp calls the API
    server-side, so neither needs CORS. A wildcard origin would let any web page the user opens
    drive the unauthenticated write endpoints (ingestion, job retry, review) of a local API.
    """
    raw = os.getenv("AEC_CORS_ORIGINS", "") if value is None else value
    return [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip() and origin.strip() != "*"]


def _job_uuid(job_id: str) -> str:
    try:
        return str(uuid.UUID(job_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings or Settings.from_env()
    db = Database(current_settings.dsn)

    app = FastAPI(
        title="AEC Intelligence Operational API",
        description="Source-grounded architectural drawing ontology and GraphRAG operational service",
        version="0.2.0",
    )

    # Added before CORS so CORS stays the outermost layer and can still answer preflights and
    # decorate 401 responses for allowed origins.
    token = api_token_from_env()
    if token:
        app.add_middleware(BearerTokenMiddleware, token=token)

    origins = cors_origins_from_env()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "Authorization"],
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/stats")
    def get_stats() -> dict[str, Any]:
        with db.connect() as conn:
            docs_count = conn.execute("SELECT count(*) as c FROM aec.documents").fetchone()["c"]
            objs_count = conn.execute("SELECT count(*) as c FROM aec.objects").fetchone()["c"]
            rels_count = conn.execute("SELECT count(*) as c FROM aec.relations").fetchone()["c"]
            embs_count = conn.execute("SELECT count(*) as c FROM aec.embeddings").fetchone()["c"]
            jobs_stat = conn.execute("SELECT state, count(*) as c FROM aec.jobs GROUP BY state").fetchall()
        return {
            "documents": docs_count,
            "objects": objs_count,
            "relations": rels_count,
            "embeddings": embs_count,
            "jobs_by_state": {row["state"]: row["c"] for row in jobs_stat},
        }

    @app.post("/v1/ingestions", status_code=status.HTTP_202_ACCEPTED)
    def enqueue_ingestion(req: IngestRequest) -> dict[str, Any]:
        # The API must honour AEC_IMPORT_ROOTS like the census pipeline does; otherwise any caller
        # could make the workers read (and copy into artifacts) arbitrary files on the host.
        try:
            target_path = current_settings.allowed_source(req.path)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Target path not found: {req.path}") from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=403,
                detail="Target path is outside the configured import roots (AEC_IMPORT_ROOTS)",
            ) from exc

        files_to_enqueue: list[Path] = []
        if target_path.is_file():
            if target_path.suffix.lower() not in SUPPORTED:
                raise HTTPException(status_code=400, detail=f"Unsupported file format: {target_path.suffix}")
            files_to_enqueue.append(target_path)
        else:
            for item in target_path.rglob("*"):
                if not (item.is_file() and item.suffix.lower() in SUPPORTED):
                    continue
                try:  # a symlink inside an import root may still point outside of it
                    files_to_enqueue.append(current_settings.allowed_source(str(item)))
                except (OSError, ValueError):
                    continue

        if not files_to_enqueue:
            raise HTTPException(status_code=400, detail="No supported CAD, PDF, IFC, or image files found")

        enqueued_jobs = []
        for file_path in files_to_enqueue:
            payload, dedup_key = ingest_job(
                file_path, project_id=req.project_id, discipline=req.discipline, queue=req.queue
            )
            job_row = db.enqueue(payload, dedup_key)
            enqueued_jobs.append(str(job_row["id"]))

        return {
            "status": "ENQUEUED",
            "enqueued_count": len(enqueued_jobs),
            "job_ids": enqueued_jobs,
        }

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job_uuid = _job_uuid(job_id)  # a malformed id is a 404, not a database error (500)
        with db.connect() as conn:
            job = conn.execute("SELECT * FROM aec.jobs WHERE id = %s", (job_uuid,)).fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        # Format datetimes
        res = dict(job)
        res["id"] = str(res["id"])
        res["created_at"] = res["created_at"].isoformat() if res.get("created_at") else None
        res["updated_at"] = res["updated_at"].isoformat() if res.get("updated_at") else None
        res["lease_until"] = res["lease_until"].isoformat() if res.get("lease_until") else None
        return res

    @app.post("/v1/jobs/{job_id}/retry")
    def retry_job(job_id: str) -> dict[str, Any]:
        job_uuid = _job_uuid(job_id)
        with db.connect() as conn:
            row = conn.execute(
                """UPDATE aec.jobs
                   SET state = 'QUEUED', attempts = 0, lease_owner = NULL, lease_until = NULL,
                       error = NULL, updated_at = now()
                   WHERE id = %s RETURNING *""",
                (job_uuid,),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"status": "RETRIED", "job_id": str(row["id"])}

    @app.post("/v1/search")
    def search(req: SearchRequest) -> dict[str, Any]:
        router = SearchRouter(db, current_settings)
        result = router.search(
            query=req.query,
            project_id=req.project_id,
            discipline=req.discipline,
            storey=normalize_storey(req.storey),
            revision=req.revision,
            kind=req.kind,
            top_k=req.top_k,
            expand_graph=req.expand_graph,
        )
        return result.to_dict()

    @app.get("/v1/objects/{object_id}")
    def get_object(object_id: str) -> dict[str, Any]:
        with db.connect() as conn:
            obj = conn.execute(
                """SELECT o.*, d.name as document_name, ST_AsGeoJSON(o.bounds)::jsonb as bounds_geojson
                   FROM aec.objects o
                   JOIN aec.documents d ON o.document_id = d.id
                   WHERE o.id = %s""",
                (object_id,),
            ).fetchone()
            if not obj:
                raise HTTPException(status_code=404, detail="Object not found")

            # Relations from SQL
            rels = conn.execute(
                """SELECT * FROM aec.relations
                   WHERE project_id = %s AND (subject = %s OR object = %s)""",
                (obj["project_id"], object_id, object_id),
            ).fetchall()

        return {
            "id": obj["id"],
            "project_id": obj["project_id"],
            "document_id": obj["document_id"],
            "document_name": obj["document_name"],
            "revision": obj["revision"],
            "kind": obj["kind"],
            "discipline": obj["discipline"],
            "storey": obj["storey"],
            "label": obj["label"],
            "search_text": obj["search_text"],
            "payload": obj["payload"],
            "bounds_geojson": obj.get("bounds_geojson"),
            "units": obj["units"],
            "relations": [dict(r) for r in rels],
        }

    # --- Element catalog: discovery endpoints for agents / power-cad-mcp -------------
    def _catalog_call(fn, *args, **kwargs):
        try:
            return fn(db, *args, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/catalog")
    def get_catalog(project_id: str | None = None) -> dict[str, Any]:
        """Table of contents: counts by kind / drawing category / layer / block with Korean aliases."""
        return _catalog_call(catalog.element_catalog, project_id=project_id)

    @app.get("/v1/elements")
    def list_elements(
        kind: str | None = Query(default=None, description="Kind or Korean alias, comma separated (e.g. Door,창호)"),
        project_id: str | None = None,
        document_id: str | None = None,
        drawing_category: str | None = Query(default=None, description="평면도/입면도/단면도/상세도/창호도 or plan/elevation/..."),
        layer: str | None = Query(default=None, description="Exact layer (case-insensitive) or wildcard A-WALL*"),
        block_name: str | None = Query(default=None, description="Block (effective) name, wildcards allowed"),
        text: str | None = Query(default=None, description="Substring of label, search text or attribute values"),
        bbox: str | None = Query(default=None, description="min_x,min_y,max_x,max_y in drawing coordinates"),
        state: str | None = None,
        storey: str | None = Query(default=None, description="Storey such as 2F, 2층, B1, 지하1층, RF (normalised server-side)"),
        include_properties: bool = False,
        limit: int = Query(default=catalog.DEFAULT_LIMIT, ge=1, le=catalog.MAX_LIMIT),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _catalog_call(catalog.find_elements, kind=kind, project_id=project_id, document_id=document_id,
                             drawing_category=drawing_category, layer=layer, block_name=block_name, text=text,
                             bbox=bbox, state=state, include_properties=include_properties, limit=limit, cursor=cursor,
                             storey=storey)

    @app.get("/v1/blocks")
    def list_blocks(
        project_id: str | None = None,
        name_like: str | None = Query(default=None, description="Substring or wildcard pattern of the block name"),
        limit: int = Query(default=100, ge=1, le=catalog.MAX_LIMIT),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _catalog_call(catalog.block_catalog, project_id=project_id, name_like=name_like, limit=limit, cursor=cursor)

    @app.get("/v1/drawings")
    def list_drawings(
        project_id: str | None = None,
        category: str | None = Query(default=None, description="Drawing category filter (평면도, 상세도, detail ...)"),
        limit: int = Query(default=100, ge=1, le=catalog.MAX_LIMIT),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _catalog_call(catalog.drawing_index, project_id=project_id, category=category, limit=limit, cursor=cursor)

    @app.get("/v1/elements/{object_id}/context")
    def get_element_context(object_id: str, hops: int = Query(default=1, ge=1, le=2),
                            limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
        result = _catalog_call(catalog.element_context, object_id, hops=hops, limit=limit)
        if result is None:
            raise HTTPException(status_code=404, detail="Object not found")
        return result

    @app.get("/v1/reviews")
    def list_review_candidates(project_id: str | None = None) -> list[dict[str, Any]]:
        with db.connect() as conn:
            query = """SELECT o.id, o.project_id, o.document_id, d.name as document_name,
                              o.revision, o.kind, o.label, o.payload
                       FROM aec.objects o
                       JOIN aec.documents d ON o.document_id = d.id
                       WHERE (o.payload->>'state') = 'AI_INFERRED'"""
            params: list[Any] = []
            if project_id:
                query += " AND o.project_id = %s"
                params.append(project_id)
            query += " LIMIT 100"
            rows = conn.execute(query, params).fetchall()

        return [
            {
                "object_id": r["id"],
                "project_id": r["project_id"],
                "document_id": r["document_id"],
                "document_name": r["document_name"],
                "revision": r["revision"],
                "kind": r["kind"],
                "label": r["label"],
                "properties": (r["payload"] or {}).get("properties", {}),
            }
            for r in rows
        ]

    @app.post("/v1/reviews")
    def review_object(action: ReviewAction) -> dict[str, Any]:
        """Applies a human review decision without mutating raw baseline.
        Creates an incremented revision of the snapshot and projects it to the DB."""
        with db.connect() as conn:
            obj = conn.execute(
                "SELECT document_id, revision, project_id FROM aec.objects WHERE id = %s",
                (action.object_id,),
            ).fetchone()
            if not obj:
                raise HTTPException(status_code=404, detail="Object not found")

            doc_id = obj["document_id"]
            current_rev = obj["revision"]

        # Read current snapshot JSON
        snapshot_dir = current_settings.data_root / "snapshots" / doc_id
        current_file = snapshot_dir / f"rev-{current_rev}.json"
        if not current_file.exists():
            raise HTTPException(status_code=404, detail="Snapshot baseline file not found")

        snapshot = json.loads(current_file.read_text(encoding="utf-8"))
        next_rev = current_rev + 1

        # Modify snapshot objects
        target_found = False
        for o in snapshot.get("objects", []):
            if o["id"] == action.object_id:
                target_found = True
                if action.action == "CONFIRM":
                    o["state"] = "USER_CONFIRMED"
                elif action.action == "CHANGE_TYPE":
                    o["state"] = "USER_CONFIRMED"
                    if action.new_type:
                        o["type"] = action.new_type
                        o["search_text"] = f"{o.get('label', '')} {action.new_type}"
                elif action.action == "REJECT":
                    o["state"] = "REJECTED"

                props = o.setdefault("properties", {})
                props["human_review"] = {
                    "action": action.action,
                    "notes": action.notes or "",
                    "prior_revision": current_rev,
                }
                break

        if not target_found:
            raise HTTPException(status_code=404, detail="Object not found in snapshot objects")

        snapshot["revision"] = next_rev
        new_file = snapshot_dir / f"rev-{next_rev}.json"
        new_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        relative_snapshot_path = str(new_file.relative_to(current_settings.data_root))

        # Project new revision to PostgreSQL + AGE
        with db.connect() as conn:
            db.project(conn, snapshot, relative_snapshot_path)

        return {
            "status": "REVIEWED",
            "document_id": doc_id,
            "previous_revision": current_rev,
            "new_revision": next_rev,
            "action": action.action,
        }

    # Serve static frontend dashboard
    web_dir = Path(__file__).parent / "web"
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

        @app.get("/", response_class=HTMLResponse)
        @app.get("/dashboard", response_class=HTMLResponse)
        def serve_dashboard():
            index_path = web_dir / "index.html"
            if index_path.exists():
                return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
            return HTMLResponse(content="<h1>AEC Operational Dashboard</h1><p>index.html not found</p>")

    return app
