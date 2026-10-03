"""Multi-stage architectural search router combining pg_trgm, pgvector, Apache AGE, and PostGIS."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..classifier import normalize_storey, storey_tokens
from .config import Settings
from .db import Database, graph_name
from .embeddings import EmbeddingService, vector_literal

# Storey recognition is canonicalized in aec_intelligence.classifier (PR #19).
# This helper only renders one already-normalized storey ID as a PostgreSQL regex,
# including legacy rows whose o.storey is blank and floor text remains in the document/search text.
def storey_pattern(level: str) -> str:
    if level == "RF":
        body = r"(옥상|옥탑|지붕|rf|roof)"
    elif level.startswith("B") and level[1:].isdigit():
        n = level[1:]
        body = f"(지하 ?0*{n} ?층?|b0*{n} ?(f|fl)?)"
    elif level.endswith("F") and level[:-1].isdigit():
        n = level[:-1]
        body = f"(0*{n} ?(층|fl|f))"
    else:
        raise ValueError(f"unsupported normalized storey: {level}")
    return f"(^|[^0-9a-z가-힣]){body}(?![0-9a-z])"


# Query intent vocabulary. Storey parsing itself stays centralized in classifier.py.
_ROOM_WORDS = {"방", "실", "룸", "공간", "방들", "room", "rooms", "space", "spaces"}
_LIST_WORDS = {"목록", "리스트", "전체", "모든", "모두", "list", "all"}
_SYNONYMS = {
    "h형강": ["H-", "형강"],
    "c형강": ["C-", "형강"],
    "ㄷ형강": ["C-", "형강"],
    "ㄱ형강": ["L-", "형강"],
    "앵글": ["L-", "형강"],
}


@dataclass
class ParsedQuery:
    terms: list[str]
    storey: str | None = None
    kind: str | None = None


def parse_query(query: str) -> ParsedQuery:
    """Split a Korean AEC question into lexical terms plus canonical storey/kind intents."""
    storey = None
    found_storeys = storey_tokens(query)
    if len(found_storeys) == 1:
        storey = next(iter(found_storeys))
        query = re.sub(storey_pattern(storey), " ", query, flags=re.IGNORECASE)

    kind = None
    terms: list[str] = []
    for token in query.split():
        low = token.lower()
        if low in _ROOM_WORDS:
            kind = "Space"
        elif low not in _LIST_WORDS:
            terms.extend(_SYNONYMS.get(low, [token]))
    return ParsedQuery(terms=terms, storey=storey, kind=kind)


# Drafting primitives that restate what a typed object already says (the "침실1" text next to the Space
# derived from it). On equal text match the typed object is the answer, so it gets a small bonus.
GENERIC_KINDS = ("Annotation", "Layer", "BlockDefinition", "Dimension", "CADEntity", "View", "Page", "Document")
GENERIC_KINDS_SQL = "(" + ", ".join(f"'{k}'" for k in GENERIC_KINDS) + ")"
DOMAIN_KIND_BONUS = 0.05


def like_pattern(term: str) -> str:
    """ILIKE pattern that matches the term literally (%, _ and \\ in the query are not wildcards)."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


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

        parsed = parse_query(query)
        effective_kind = kind or parsed.kind
        storey_level = normalize_storey(storey) if storey else parsed.storey

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
            if storey and not storey_level:
                where_clauses.append("o.storey = %s")
                params.append(storey)
            elif storey_level:
                # Objects usually carry no storey; the floor is in the drawing name ("A-101_1층평면도").
                where_clauses.append("(o.storey ~* %s OR (COALESCE(o.storey, '') = '' "
                                     "AND (d.name || ' ' || o.search_text) ~* %s))")
                params.extend([storey_pattern(storey_level)] * 2)
            if revision is not None:
                where_clauses.append("o.revision = %s")
                params.append(revision)
            if effective_kind:
                where_clauses.append("o.kind = %s")
                params.append(effective_kind)

            where_sql = " AND ".join(where_clauses)

            # 2. Lexical score: each query term scores 1 when the text contains it literally, else its
            # word_similarity. Plain similarity() compares the short query with the whole (long) search
            # text, so even an exact Korean room name scored about 0.05 and lost to unrelated vector hits.
            terms = parsed.terms
            if terms:
                # Fuzzy credit stays below an exact hit, and needs 3+ letters: pg_trgm drops punctuation,
                # so "H-" or "100%" would otherwise fuzzily "match" any word starting with h or 100.
                lexical_sql = "(" + " + ".join(
                    "(CASE WHEN o.search_text ILIKE %s THEN 1.0 ELSE 0.8 * word_similarity(%s, o.search_text) END)"
                    if len(re.sub(r"\W", "", t)) >= 3 else "(CASE WHEN o.search_text ILIKE %s THEN 1.0 ELSE 0.0 END)"
                    for t in terms) + f") / {len(terms)}"
                lexical_params: list[Any] = [p for t in terms
                                             for p in ((like_pattern(t), t) if len(re.sub(r"\W", "", t)) >= 3
                                                       else (like_pattern(t),))]
                match_sql = ("(" + " OR ".join("o.search_text ILIKE %s" for _ in terms)
                             + " OR word_similarity(%s, o.search_text) > 0.3"
                             + " OR (e.embedding IS NOT NULL AND (e.embedding <=> %s::vector) < 0.6))")
                match_params: list[Any] = [like_pattern(t) for t in terms] + [" ".join(terms), vec_str]
                lexical_weight = 0.6
            else:
                # Only intents ("2층 방"): the storey and kind filters are the whole question.
                lexical_sql, lexical_params = "1.0", []
                match_sql, match_params = "TRUE", []
                if not (storey_level or effective_kind):
                    lexical_sql = "0.0"
                lexical_weight = 0.6
            vector_weight = 1.0 - lexical_weight

            sql_query = f"""
                SELECT * FROM (
                    SELECT
                        o.id, o.project_id, o.document_id, d.name as document_name, o.revision, o.kind,
                        o.discipline, o.storey, o.label, o.search_text, o.payload,
                        {lexical_sql} as lexical_score,
                        COALESCE(1.0 - (e.embedding <=> %s::vector), 0.0) as vector_score,
                        CASE WHEN o.kind IN {GENERIC_KINDS_SQL} THEN 0.0 ELSE {DOMAIN_KIND_BONUS} END as kind_prior
                    FROM aec.objects o
                    JOIN aec.documents d ON o.document_id = d.id
                    LEFT JOIN aec.embeddings e ON o.id = e.object_id AND e.model = %s
                    WHERE {where_sql} AND {match_sql}
                ) ranked
                ORDER BY lexical_score * {lexical_weight} + vector_score * {vector_weight} + kind_prior DESC, label
                LIMIT %s
            """
            full_params = [*lexical_params, vec_str, query_model, *params, *match_params, top_k * 2]

            try:
                with conn.transaction():
                    rows = conn.execute(sql_query, full_params).fetchall()
            except Exception as exc:
                # If vector extension or age is absent in light test DB, fallback to simple ILIKE.
                # The savepoint above keeps the connection usable after the failed statement.
                if terms:
                    fallback_match = "(" + " OR ".join("o.search_text ILIKE %s" for _ in terms) + ")"
                    fallback_match_params = [like_pattern(term) for term in terms]
                else:
                    fallback_match = "TRUE"
                    fallback_match_params = []
                fallback_sql = f"""
                    SELECT
                        o.id, o.project_id, o.document_id, d.name as document_name,
                        o.revision, o.kind, o.discipline, o.storey, o.label, o.search_text, o.payload,
                        1.0 as lexical_score, 0.0 as vector_score, 0.0 as kind_prior
                    FROM aec.objects o
                    JOIN aec.documents d ON o.document_id = d.id
                    WHERE {where_sql} AND {fallback_match}
                    LIMIT %s
                """
                rows = conn.execute(
                    fallback_sql, [*params, *fallback_match_params, top_k]
                ).fetchall()
                warnings.append(f"Semantic vector search fell back to lexical search: {exc}")

            hits: list[SearchHit] = []
            for row in rows[:top_k]:
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
                combined_score = lex_score * lexical_weight + vec_score * vector_weight + float(row.get("kind_prior") or 0.0)

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
        """Expands relations using Apache AGE graph where possible, falling back to SQL relations table.

        Each step runs in its own savepoint: a failed Cypher call would otherwise abort the search
        transaction, so the SQL fallback and every later hit silently came back with no relations.
        """
        # 1. Apache AGE: db.cypher declares a single agtype column, so the row is returned as one map.
        g_name = graph_name(project_id)
        try:
            query = (f"MATCH (a:Entity)-[r:Rel]-(b:Entity) WHERE a.id = {json.dumps(object_id, ensure_ascii=False)} "
                     "RETURN {target: b.id, target_kind: b.kind, predicate: r.kind, "
                     "outgoing: id(startNode(r)) = id(a)} LIMIT 20")
            with conn.transaction():
                cypher_res = self.db.cypher(conn, g_name, query)
            rels = []
            for row in cypher_res:
                edge = json.loads(str(row["value"]))
                subject, obj = (object_id, edge["target"]) if edge["outgoing"] else (edge["target"], object_id)
                rels.append({"subject": subject, "predicate": edge["predicate"], "object": obj,
                             "target_kind": edge["target_kind"], "state": "OBSERVED", "source": "AGE"})
            if rels:
                return rels
        except Exception:
            pass

        # 2. SQL relation table fallback (also holds AI_INFERRED links that are not projected to AGE)
        try:
            with conn.transaction():
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
                    "source": "SQL",
                }
                for r in rows
            ]
        except Exception:
            return []
