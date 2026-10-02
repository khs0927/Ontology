"""Multi-stage architectural search router combining pg_trgm, pgvector, Apache AGE, and PostGIS."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .db import Database, graph_name
from .embeddings import EmbeddingService, vector_literal

# Query words that name the kind of object asked for ("1층 방 목록" asks for Space objects).
KIND_INTENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Space", re.compile(r"방|실(?:명|이름|목록)?(?:\s|$)|호실|공간|\broom|\bspace", re.IGNORECASE)),
    ("SteelSection", re.compile(r"(?:^|[\s(])(?:H|C|L|I|□|ㅁ|RHS|SHS|CHS|PIPE)\s*-\s*\d|형강|철골|단면\s*규격|steel|section",
                                re.IGNORECASE)),
    ("Storey", re.compile(r"층\s*(?:목록|정보)|\bstorey|\bfloors?\b", re.IGNORECASE)),
    ("Door", re.compile(r"(?:^|\s)문(?:\s|$)|출입문|\bdoor", re.IGNORECASE)),
    ("Window", re.compile(r"창호|(?:^|\s)창(?:\s|$)|\bwindow", re.IGNORECASE)),
    ("Wall", re.compile(r"벽|\bwall", re.IGNORECASE)),
    ("Column", re.compile(r"기둥|\bcolumn", re.IGNORECASE)),
    ("Stair", re.compile(r"계단|\bstair", re.IGNORECASE)),
    ("TitleBlock", re.compile(r"표제란|도곽|title\s*block", re.IGNORECASE)),
)
_STOREY_QUERY = (
    (re.compile(r"지하\s*(\d{1,2})\s*층"), lambda m: f"B{m.group(1)}"),
    (re.compile(r"\bB(\d{1,2})F?\b", re.IGNORECASE), lambda m: f"B{m.group(1)}"),
    (re.compile(r"(\d{1,3})\s*층"), lambda m: f"{m.group(1)}F"),
    (re.compile(r"\b(\d{1,3})F\b", re.IGNORECASE), lambda m: f"{m.group(1)}F"),
)
_NAME_KEYS = ("roomName", "roomNumber", "sectionDesignation", "drawingTitle", "drawingNumber", "storeyName", "name", "mark")
# Generic words that should not count as a name match on their own.
_STOPWORDS = {"목록", "리스트", "list", "전체", "모든", "all", "도면", "평면도", "the", "of", "in"}
EXACT_NAME_BOOST = 1.0
SUBSTRING_BOOST = 0.5
TOKEN_BOOST = 0.1
KIND_INTENT_BOOST = 0.3
STOREY_BOOST = 0.2


def kind_intents(query: str) -> list[str]:
    return [kind for kind, pattern in KIND_INTENTS if pattern.search(query)]


def storey_intent(query: str) -> str | None:
    for pattern, build in _STOREY_QUERY:
        found = pattern.search(query)
        if found:
            return build(found)
    return None


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def ranking_boost(query: str, kind: str, label: str, properties: dict[str, Any], storey: str = "",
                  intents: list[str] | None = None, storey_hint: str | None = None) -> float:
    """Boost for direct name matches and for the kind/storey the query asks about.

    A literal match (a room called '화장실', the section 'H-400x200') beats a merely similar vector,
    and '1층 방 목록' prefers Space objects on storey 1F over semantically close annotations.
    """
    intents = kind_intents(query) if intents is None else intents
    storey_hint = storey_intent(query) if storey_hint is None else storey_hint
    q = _norm(query)
    names = [_norm(properties.get(k)) for k in _NAME_KEYS if properties.get(k)]
    boost = 0.0
    tokens = [t for t in re.split(r"\s+", query.strip()) if len(t) >= 2 and t.casefold() not in _STOPWORDS]
    if q and any(n == q for n in names):
        boost += EXACT_NAME_BOOST
    elif q and (any(n == _norm(t) for n in names for t in tokens)):
        boost += EXACT_NAME_BOOST * 0.8
    elif q and q in _norm(label):
        boost += SUBSTRING_BOOST
    else:
        hay = _norm(label) + " " + " ".join(names)
        boost += min(3, sum(1 for t in tokens if _norm(t) in hay)) * TOKEN_BOOST
    if kind in intents:
        boost += KIND_INTENT_BOOST
    if storey_hint and storey == storey_hint:
        boost += STOREY_BOOST
    return boost


@dataclass
class Citation:
    document_id: str
    document_name: str
    revision: int
    layout_or_page: str
    handle_or_id: str
    coordinate_system: str
    bbox: list[float] | None = None
    preview_path: str | None = None
    state: str = "OBSERVED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_name": self.document_name,
            "revision": self.revision,
            "layout_or_page": self.layout_or_page,
            "handle_or_id": self.handle_or_id,
            "coordinate_system": self.coordinate_system,
            "bbox": self.bbox,
            "preview_path": self.preview_path,
            "state": self.state,
        }


@dataclass
class SearchHit:
    object_id: str
    project_id: str
    kind: str
    label: str
    discipline: str
    storey: str
    revision: int
    score: float
    lexical_score: float
    vector_score: float
    citation: Citation
    properties: dict[str, Any] = field(default_factory=dict)
    relations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "project_id": self.project_id,
            "kind": self.kind,
            "label": self.label,
            "discipline": self.discipline,
            "storey": self.storey,
            "revision": self.revision,
            "score": round(self.score, 4),
            "lexical_score": round(self.lexical_score, 4),
            "vector_score": round(self.vector_score, 4),
            "citation": self.citation.to_dict(),
            "properties": self.properties,
            "relations": self.relations,
        }


@dataclass
class SearchResult:
    query: str
    total_hits: int
    hits: list[SearchHit]
    warnings: list[str] = field(default_factory=list)
    unresolved_candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "total_hits": self.total_hits,
            "hits": [h.to_dict() for h in self.hits],
            "warnings": self.warnings,
            "unresolved_candidates": self.unresolved_candidates,
        }


class SearchRouter:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.embedding_service = EmbeddingService(settings)

    def search(
        self,
        query: str,
        project_id: str | None = None,
        discipline: str | None = None,
        storey: str | None = None,
        revision: int | None = None,
        kind: str | None = None,
        top_k: int = 10,
        expand_graph: bool = True,
    ) -> SearchResult:
        """Executes multi-stage hybrid search:
        1. Exact code & pg_trgm lexical match
        2. pgvector semantic similarity match
        3. Reciprocal Rank Fusion / Weighted Scoring
        4. Apache AGE graph relation expansion
        5. Citation & provenance extraction
        """
        warnings: list[str] = []
        unresolved: list[dict[str, Any]] = []

        # Only vectors produced by the same model as the query vector are comparable.
        query_model, query_vecs = self.embedding_service.embed_with_model([query])
        vec_str = vector_literal(query_vecs[0])
        if query_model != self.embedding_service.active_model():
            warnings.append(
                f"Embedding endpoint unavailable ({self.embedding_service.last_error}); "
                f"vector stage limited to '{query_model}' vectors"
            )

        with self.db.connect() as conn:
            # 1. Base SQL filter conditions
            where_clauses = ["1=1"]
            params: list[Any] = []
            if project_id:
                where_clauses.append("o.project_id = %s")
                params.append(project_id)
            if discipline:
                where_clauses.append("o.discipline = %s")
                params.append(discipline)
            if storey:
                where_clauses.append("o.storey = %s")
                params.append(storey)
            if revision is not None:
                where_clauses.append("o.revision = %s")
                params.append(revision)
            if kind:
                where_clauses.append("o.kind = %s")
                params.append(kind)

            where_sql = " AND ".join(where_clauses)
            intents = kind_intents(query)
            storey_hint = storey_intent(query)
            tokens = [t for t in query.split() if len(t) >= 2 and t.casefold() not in _STOPWORDS]
            token_patterns = [f"%{t}%" for t in tokens] or [f"%{query}%"]

            # 2. Hybrid search query: combines pg_trgm similarity and pgvector distance
            # Checks if embeddings exist; if not, falls back smoothly to lexical alone
            sql_query = f"""
                SELECT
                    o.id,
                    o.project_id,
                    o.document_id,
                    d.name as document_name,
                    o.revision,
                    o.kind,
                    o.discipline,
                    o.storey,
                    o.label,
                    o.search_text,
                    o.payload,
                    COALESCE(similarity(o.search_text, %s), 0.0) as lexical_score,
                    COALESCE(1.0 - (e.embedding <=> %s::vector), 0.0) as vector_score
                FROM aec.objects o
                JOIN aec.documents d ON o.document_id = d.id
                LEFT JOIN aec.embeddings e ON o.id = e.object_id AND e.model = %s
                WHERE {where_sql}
                  AND (
                    o.search_text ILIKE %s
                    OR similarity(o.search_text, %s) > 0.1
                    OR (e.embedding IS NOT NULL AND (e.embedding <=> %s::vector) < 0.6)
                    OR (o.kind = ANY(%s::text[]) AND (o.search_text ILIKE ANY(%s::text[]) OR o.storey = %s))
                  )
                ORDER BY (COALESCE(similarity(o.search_text, %s), 0.0) * 0.4 +
                          COALESCE(1.0 - (e.embedding <=> %s::vector), 0.0) * 0.6 +
                          CASE WHEN o.label ILIKE %s THEN {SUBSTRING_BOOST} ELSE 0 END +
                          CASE WHEN o.kind = ANY(%s::text[]) THEN {KIND_INTENT_BOOST} ELSE 0 END +
                          CASE WHEN o.storey = %s THEN {STOREY_BOOST} ELSE 0 END) DESC
                LIMIT %s
            """

            like_pattern = f"%{query}%"
            full_params = [
                query, vec_str, query_model, *params,
                like_pattern, query, vec_str, intents, token_patterns, storey_hint,
                query, vec_str, like_pattern, intents, storey_hint, max(top_k * 4, 20)
            ]

            try:
                with conn.transaction():
                    rows = conn.execute(sql_query, full_params).fetchall()
            except Exception as exc:
                # If vector extension or age is absent in light test DB, fallback to simple ILIKE
                fallback_sql = f"""
                    SELECT
                        o.id, o.project_id, o.document_id, d.name as document_name,
                        o.revision, o.kind, o.discipline, o.storey, o.label, o.search_text, o.payload,
                        1.0 as lexical_score, 0.0 as vector_score
                    FROM aec.objects o
                    JOIN aec.documents d ON o.document_id = d.id
                    WHERE {where_sql} AND o.search_text ILIKE %s
                    LIMIT %s
                """
                rows = conn.execute(fallback_sql, [*params, like_pattern, top_k]).fetchall()
                warnings.append(f"Semantic vector search fell back to lexical search: {exc}")

            def _score(row):
                payload = row.get("payload") or {}
                base = float(row.get("lexical_score") or 0.0) * 0.4 + float(row.get("vector_score") or 0.0) * 0.6
                return base + ranking_boost(query, row["kind"], row["label"] or "", payload.get("properties") or {},
                                            row.get("storey") or "", intents, storey_hint)

            scored = sorted(((_score(row), index, row) for index, row in enumerate(rows)), key=lambda s: (-s[0], s[1]))
            hits: list[SearchHit] = []
            for combined_score, _, row in scored[:top_k]:
                payload = row.get("payload") or {}
                evidence = payload.get("evidence") or {}
                props = payload.get("properties") or {}
                state = payload.get("state") or "OBSERVED"

                if state == "AI_INFERRED":
                    unresolved.append({
                        "object_id": row["id"],
                        "kind": row["kind"],
                        "label": row["label"],
                        "reason": "AI inferred candidate; requires human review"
                    })

                bbox_dict = payload.get("bbox") or {}
                bbox_list = None
                if all(k in bbox_dict for k in ("min_x", "min_y", "max_x", "max_y")):
                    bbox_list = [bbox_dict["min_x"], bbox_dict["min_y"], bbox_dict["max_x"], bbox_dict["max_y"]]

                citation = Citation(
                    document_id=row["document_id"],
                    document_name=row["document_name"],
                    revision=row["revision"],
                    layout_or_page=str(evidence.get("layout") or evidence.get("page") or "default"),
                    handle_or_id=str(evidence.get("handle") or row["id"]),
                    coordinate_system=evidence.get("coordinate_system", "CAD_WCS"),
                    bbox=bbox_list,
                    preview_path=evidence.get("preview_path"),
                    state=state,
                )

                lex_score = float(row.get("lexical_score") or 0.0)
                vec_score = float(row.get("vector_score") or 0.0)

                relations = []
                if expand_graph:
                    relations = self._get_relations(conn, row["project_id"], row["id"])

                hit = SearchHit(
                    object_id=row["id"],
                    project_id=row["project_id"],
                    kind=row["kind"],
                    label=row["label"],
                    discipline=row["discipline"] or "",
                    storey=row["storey"] or "",
                    revision=row["revision"],
                    score=combined_score,
                    lexical_score=lex_score,
                    vector_score=vec_score,
                    citation=citation,
                    properties=props,
                    relations=relations,
                )
                hits.append(hit)

        return SearchResult(
            query=query,
            total_hits=len(hits),
            hits=hits,
            warnings=warnings,
            unresolved_candidates=unresolved,
        )

    def _get_relations(self, conn, project_id: str, object_id: str) -> list[dict[str, Any]]:
        """Expands relations using Apache AGE graph where possible, falling back to SQL relations table."""
        # 1. Try Apache AGE Cypher query
        g_name = graph_name(project_id)
        try:
            query = (f"MATCH (a:Entity)-[r:Rel]-(b:Entity) WHERE a.id = {json.dumps(object_id)} "
                     "RETURN [b.id, b.kind, b.name, r.kind, r.state, r.confidence] LIMIT 20")
            with conn.transaction():  # a missing graph must not abort the search transaction
                cypher_res = self.db.cypher(conn, g_name, query)
            if cypher_res:
                rels = []
                for row in cypher_res:
                    target, kind, name, predicate, state, confidence = json.loads(str(row["value"]))
                    rels.append({"target": target, "target_kind": kind, "target_name": name, "predicate": predicate,
                                 "state": state, "confidence": confidence, "type": "AGE_REL"})
                return rels
        except Exception:
            pass

        # 2. SQL relation table fallback
        try:
            rows = conn.execute(
                """SELECT subject, predicate, object, state, evidence
                   FROM aec.relations
                   WHERE project_id = %s AND (subject = %s OR object = %s)
                   LIMIT 20""",
                (project_id, object_id, object_id),
            ).fetchall()
            return [
                {
                    "subject": r["subject"],
                    "predicate": r["predicate"],
                    "object": r["object"],
                    "state": r["state"],
                    "evidence": r["evidence"],
                }
                for r in rows
            ]
        except Exception:
            return []
