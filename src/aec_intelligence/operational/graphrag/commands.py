"""CLI sub-commands for Phase 4 (registered by operational.cli)."""

from __future__ import annotations

import json
import sys


def add_parsers(subparsers) -> None:
    p = subparsers.add_parser("kg-build", help="Build/refresh the canonical knowledge graph (aec.kg_*)")
    p.add_argument("--project", default=None, help="Only this canonical project key")
    p.add_argument("--force", action="store_true", help="Rebuild even when the project fingerprint is unchanged")
    p.add_argument("--steel-catalog", default=None, help="hs-steel attributes folder (else AEC_STEEL_CATALOG_DIR)")
    p.add_argument("--rules", default=None, help="Requirements JSON (else AEC_RULES_FILE)")

    p = subparsers.add_parser("kg-summarize", help="Detect communities and summarise them with the local LLM")
    p.add_argument("--project", default=None)
    p.add_argument("--leiden", action="store_true", help="Add Leiden communities (needs python-igraph)")
    p.add_argument("--limit", type=int, default=None, help="Summarise at most N communities this run")
    p.add_argument("--max-level", type=int, default=1, help="Summarise levels <= N (0 project, 1 aspects, 2 leiden)")
    p.add_argument("--no-llm", action="store_true", help="Only refresh communities/facts, no summaries")

    p = subparsers.add_parser("kg-stats", help="Knowledge graph node/edge/community counts")

    p = subparsers.add_parser("kg-facts", help="Export rule facts + ArchOntos subject refs for one project")
    p.add_argument("project", help="Canonical project key (see kg-stats / aec.kg_build_state)")
    p.add_argument("--out", default=None, help="Write aec-facts-export/1 JSON here (default: stdout)")

    p = subparsers.add_parser("ask", help="Graph RAG question answering (local LLM, cited)")
    p.add_argument("question")
    p.add_argument("--project", default=None, help="Canonical project key or stored project id")
    p.add_argument("--top-k", type=int, default=12)
    p.add_argument("--no-llm", action="store_true", help="Retrieval + extractive answer only")
    p.add_argument("--json", action="store_true", help="Print the full JSON result")

    p = subparsers.add_parser("graphrag-eval-make", help="Write a Korean golden set from the ingested data (private)")
    p.add_argument("out", help="JSONL path on the data disk (not in git)")
    p.add_argument("--projects", type=int, default=3)
    p.add_argument("--total", type=int, default=50)

    p = subparsers.add_parser("graphrag-eval", help="Run a Graph RAG eval set (JSONL) and write a report")
    p.add_argument("eval_file")
    p.add_argument("--out", default=None, help="Report JSON path (default: next to the eval file)")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--limit", type=int, default=None)


def _print(data) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")


def cmd_kg_build(parsed, settings, db):
    from .kg import KnowledgeGraphBuilder

    db.initialize()
    result = KnowledgeGraphBuilder(db, steel_catalog_dir=parsed.steel_catalog, rules_file=parsed.rules).build(
        parsed.project, force=parsed.force)
    _print(result)
    return result


def cmd_kg_summarize(parsed, settings, db):
    from ..embeddings import EmbeddingService
    from . import communities
    from .llm import LocalLLM

    db.initialize()
    llm = None if parsed.no_llm else LocalLLM()
    refreshed = communities.refresh(db, parsed.project, leiden=parsed.leiden, model=llm.model if llm else "")
    result = {"communities": refreshed}
    if llm is not None:
        result["summaries"] = communities.summarize(db, llm, embedder=EmbeddingService(settings),
                                                    project_key=parsed.project, limit=parsed.limit,
                                                    max_level=parsed.max_level)
    _print(result)
    return result


def cmd_kg_stats(parsed, settings, db):
    from .kg import kg_stats

    result = kg_stats(db)
    _print(result)
    return result


def cmd_kg_facts(parsed, settings, db):
    from pathlib import Path

    from .integrations import project_facts

    result = project_facts(db, parsed.project)
    if result is None:
        raise SystemExit(f"unknown project key: {parsed.project}")
    if parsed.out:
        Path(parsed.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        _print({"written": parsed.out, "facts": sorted(result["facts"]), "subjects": len(result["subjects"])})
    else:
        _print(result)
    return result


def cmd_ask(parsed, settings, db):
    from .ask import GraphRAG
    from .llm import LocalLLM

    rag = GraphRAG(db, settings, llm=None if parsed.no_llm else LocalLLM())
    result = rag.ask(parsed.question, project=parsed.project, top_k=parsed.top_k, generate=not parsed.no_llm)
    if parsed.json:
        _print(result)
    else:
        sys.stdout.write(result["answer"] + "\n\n")
        for c in result["citations"]:
            sys.stdout.write(f"[{c['id']}] {c.get('document_name')} | {c.get('layout_or_page') or '-'} | "
                             f"objects {','.join(o[:16] for o in c.get('object_ids') or []) or '-'}\n")
        sys.stdout.write(f"(route {result['route']}, retrieval {result['retrieval_ms']} ms, "
                         f"llm {result.get('llm_ms', 0)} ms)\n")
    return result


def cmd_graphrag_eval(parsed, settings, db):
    from .evaluate import run_eval

    result = run_eval(db, settings, parsed.eval_file, out=parsed.out, use_llm=not parsed.no_llm,
                      limit=parsed.limit)
    _print(result["summary"])
    return result


def cmd_graphrag_eval_make(parsed, settings, db):
    from .evaluate import make_eval_set

    result = make_eval_set(db, parsed.out, projects=parsed.projects, total=parsed.total)
    _print(result)
    return result


COMMANDS = {"graphrag-eval-make": cmd_graphrag_eval_make, "kg-build": cmd_kg_build, "kg-summarize": cmd_kg_summarize, "kg-stats": cmd_kg_stats,
            "kg-facts": cmd_kg_facts,
            "ask": cmd_ask, "graphrag-eval": cmd_graphrag_eval}
