import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

PACK = Path(__file__).resolve().parents[1] / "library" / "archioffice"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, PACK / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


loader = _load("load_archioffice")
builder = _load("build_archioffice_pack")


def _vec(i, text="t"):
    return {"node_id": f"ARCHIOFFICE:x:{i}", "kind": "symbol", "label": f"L{i}",
            "text": f"{text}{i}", "content_hash": f"h-{text}{i}"}


class FakeCursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return FakeCursor(self.log)

    def commit(self):
        self.log.append(("COMMIT", None))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_db(monkeypatch):
    log = []
    mod = types.ModuleType("psycopg")
    mod.connect = lambda dsn: FakeConn(log)
    monkeypatch.setitem(sys.modules, "psycopg", mod)
    monkeypatch.setattr(loader, "embed", lambda text: [0.0] * loader.DIM)
    monkeypatch.setattr(loader, "load_jsonl", lambda p: _fake_rows(p))
    return log


def _fake_rows(path):
    if path.name == "vector_corpus.jsonl":
        return [_vec(1), _vec(2)]
    return [{"id": "ARCHIOFFICE:x:1", "type": "T", "props": {}}]


def _idx(log, needle):
    return [i for i, (s, _) in enumerate(log) if needle in s]


def test_links_run_after_object_inserts_on_clean_load(fake_db):
    assert loader.apply("postgresql://fake", sql_only=False) == 0
    log = fake_db
    kg_sql = [i for i, (s, _) in enumerate(log) if "BEGIN;" in s]
    last_obj = max(_idx(log, "INSERT INTO aec.objects"))
    links = _idx(log, "SET object_ids")
    assert kg_sql and kg_sql[0] < min(_idx(log, "INSERT INTO aec.objects"))
    assert links and max(links) > last_obj
    assert log[-1][0] == "COMMIT"


def test_sql_only_runs_script_and_links_without_objects(fake_db):
    assert loader.apply("postgresql://fake", sql_only=True) == 0
    log = fake_db
    assert not _idx(log, "INSERT INTO aec.objects")
    assert not _idx(log, "INSERT INTO aec.embeddings")
    assert _idx(log, "SET object_ids") and log[-1][0] == "COMMIT"


def test_embedding_mapping_is_upserted_in_both_branches(fake_db, monkeypatch):
    monkeypatch.setattr(loader, "load_jsonl", lambda p: (
        [_vec(1), dict(_vec(2), content_hash="h-t1")] if p.name == "vector_corpus.jsonl"
        else [{"id": "n"}]))
    loader.apply("postgresql://fake", sql_only=False)
    emb = [s for s, _ in fake_db if "INSERT INTO aec.embeddings" in s]
    assert len(emb) == 2  # new-hash branch and seen-hash branch
    for s in emb:
        assert "DO UPDATE SET revision=EXCLUDED.revision, content_hash=EXCLUDED.content_hash" in s
        assert "DO NOTHING" not in s


def test_stale_objects_pruned_only_for_archioffice_after_inserts(fake_db):
    loader.apply("postgresql://fake", sql_only=False)
    log = fake_db
    dele = _idx(log, "DELETE FROM aec.objects")
    emb = _idx(log, "DELETE FROM aec.embeddings")
    assert len(dele) == 1 and len(emb) == 1 and emb[0] < dele[0]
    assert dele[0] > max(_idx(log, "INSERT INTO aec.objects"))
    for i in (dele[0], emb[0]):
        sql, params = log[i]
        assert "project_id = %s" in sql
        assert params == ("ARCHIOFFICE", ["ARCHIOFFICE:x:1", "ARCHIOFFICE:x:2"])
    assert not _idx(log, "DELETE FROM aec.text_vectors")


@pytest.mark.parametrize("bad", [
    [], [{"node_id": "", "kind": "k", "content_hash": "h", "text": "t"}],
    [{"kind": "k", "content_hash": "h", "text": "t"}],
    [_vec(1), _vec(1)],
])
def test_invalid_corpus_never_prunes(bad):
    log = []
    with pytest.raises(ValueError):
        loader.prune_stale_objects(FakeCursor(log), bad)
    assert log == []


def test_empty_corpus_aborts_apply_before_any_write(fake_db, monkeypatch):
    monkeypatch.setattr(loader, "load_jsonl", lambda p: [])
    with pytest.raises(ValueError):
        loader.apply("postgresql://fake", sql_only=False)
    assert not _idx(fake_db, "DELETE") and not _idx(fake_db, "BEGIN;")


@pytest.mark.parametrize("text,expected", [
    ("문 1400x1250", (1400, 1250)),
    ("1400 × 1250", (1400, 1250)),
    ("1400(W) x 1250(D)", (1400, 1250)),
    ("1400(W)x1250(H)", (1400, 1250)),
    ("1400W x 1250H", (1400, 1250)),
    ("W1400 x D1250", (1400, 1250)),
    ("W: 1400 x H: 1250", (1400, 1250)),
    ("1400mm x 1250mm", (1400, 1250)),
    ("1400 mm(W) X 1250 mm(D)", (1400, 1250)),
])
def test_dimension_parser_variants(text, expected):
    m = builder.DIM_RE.search(text)
    assert m and (int(m.group(1)), int(m.group(2))) == expected


@pytest.mark.parametrize("text", ["no size", "1400", "W 1400", "12 x 3 floors"])
def test_dimension_parser_rejects_non_dimensions(text):
    assert builder.DIM_RE.search(text) is None


def test_jsonld_includes_every_edge_and_committed_artifact_matches():
    graph = PACK / "graph"
    nodes = [json.loads(l) for l in (graph / "kg_nodes.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    edges = [json.loads(l) for l in (graph / "kg_edges.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert edges
    built = builder.build_jsonld(nodes, edges)
    assert len(built["@graph"]) == len(nodes) + len(edges)
    committed = json.loads((graph / "graph.jsonld").read_text(encoding="utf-8"))
    assert len(committed["@graph"]) == len(nodes) + len(edges)
    assert committed == built


def test_main_uses_build_jsonld_and_never_slices_edges():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(builder.main).lstrip() if False else
                     (PACK / "tools" / "build_archioffice_pack.py").read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [c.func.id for c in ast.walk(main) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
    assert "build_jsonld" in calls
    slices = [s for s in ast.walk(main) if isinstance(s, ast.Subscript) and isinstance(s.slice, ast.Slice)
              and isinstance(s.value, ast.Name) and s.value.id == "edges"]
    assert not slices, "main() must not slice edges when exporting JSON-LD"
