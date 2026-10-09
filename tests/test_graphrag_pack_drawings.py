"""Ranking of drive-pack drawing nodes (GDRIVE_CAD) in Graph RAG; no database (scripted connection)."""
from aec_intelligence.operational.graphrag.ask import GraphRAG, pack_terms


class _Res:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Conn:
    """Answers by SQL shape: pack-scope probe, per-term document frequency, scoring, node fetch, storeys."""

    def __init__(self, pack=True, df=None, scores=None, nodes=None):
        self.pack, self.df, self.scores, self.nodes = pack, df or {}, scores or [], nodes or {}
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(sql)
        if "LIMIT 1" in sql and "drive_path" in sql:
            return _Res([{"?column?": 1}] if self.pack else [])
        if "count(*)" in sql:
            return _Res([{"c": self.df.get(params[1], 0)}])
        if "word_similarity" in sql:
            return _Res(self.scores)
        if "ANY(%s)" in sql and "n.props" in sql:
            return _Res([self.nodes[i] for i in params[0] if i in self.nodes])
        return _Res([])


def _node(nid, name, path, copies=()):
    return {"id": nid, "project_key": "GDRIVE_CAD", "type": "Drawing", "name": name, "object_ids": [],
            "document_ids": ["doc"], "props": {"drive_path": path, "sub_project": path.rsplit("/", 1)[0],
                                                "duplicate_copies": list(copies), "duplicate_count": len(copies)}}


def test_pack_terms_keep_decimals_and_drop_punctuation():
    assert pack_terms("L형옹벽상세도 7.0m") == ["L형옹벽상세도", "7.0m"]
    assert pack_terms("신평동 610-13 대수선 사용승인도면?") == ["신평동", "610-13", "대수선", "사용승인도면"]
    assert pack_terms("우수받이상세도.") == ["우수받이상세도"]
    assert pack_terms("1,2공장 도면 및 a") == ["2공장", "도면"]  # one-character words are dropped


def test_non_pack_projects_are_untouched():
    conn = _Conn(pack=False)
    rag = GraphRAG(None, None)
    assert rag._pack_drawings(conn, "1층 평면도", ["OTHER"]) is None
    assert rag._pack_drawings(conn, "1층 평면도", None) is None  # unscoped questions keep the old path
    assert len(conn.sql) == 1  # only the scope probe ran


def test_pack_ranking_scores_terms_separately_and_breaks_ties_by_short_name():
    nodes = {
        "a": _node("a", "A203_2층평면도", "v/dwg/A203_2층평면도.dwg"),
        "b": _node("b", "2층평면도", "v/dwg/views/2층평면도.dwg", copies=["v/other/2층평면도.dwg"]),
        "c": _node("c", "정면도", "v/dwg/views/정면도.dwg"),
    }
    scores = [
        {"id": "c", "name": "정면도", "tsim": 0.5, "wsim": 0.7, "csim": 0.4},
        {"id": "a", "name": "A203_2층평면도", "tsim": 1.0, "wsim": 0.7, "csim": 0.5},
        {"id": "b", "name": "2층평면도", "tsim": 1.0, "wsim": 0.7, "csim": 0.5},
    ]
    conn = _Conn(df={"hillside": 70, "villa": 70, "2층평면도": 40}, scores=scores, nodes=nodes)
    items = GraphRAG(None, None)._pack_drawings(conn, "hillside villa 2층평면도", ["GDRIVE_CAD"])
    assert [i.node_id for i in items] == ["b", "a", "c"]  # same score: shorter title first
    assert items[0].path == "v/dwg/views/2층평면도.dwg"
    assert items[0].copies == ["v/other/2층평면도.dwg"] and "동일 파일 사본 1건" in items[0].text
    assert items[2].copies == [] and "사본" not in items[2].text
    assert items[0].score > items[2].score


def test_common_terms_do_not_pick_candidates():
    conn = _Conn(df={"도면": 9000, "건축": 5000, "부산연구원": 12}, scores=[])
    GraphRAG(None, None)._pack_drawings(conn, "부산연구원 건축 도면", ["GDRIVE_CAD"])
    cand = next(s for s in conn.sql if "word_similarity" in s)
    assert "%%> ANY(%s::text[])" in cand  # candidates come from the rare term list only


def test_all_common_terms_fall_back_to_whole_question_matches():
    conn = _Conn(df={"평면도": 5000, "단면도": 4000}, scores=[])
    GraphRAG(None, None)._pack_drawings(conn, "평면도 단면도", ["GDRIVE_CAD"])
    cand = next(s for s in conn.sql if "word_similarity" in s)
    assert "<%% n.search_text" in cand
