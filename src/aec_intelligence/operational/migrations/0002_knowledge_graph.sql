-- Phase 4: knowledge graph (relational mirror of the canonical, entity-resolved project graph) and
-- Graph RAG caches. The per-document object graph stays in aec.objects/aec.relations and AGE.
CREATE TABLE IF NOT EXISTS aec.kg_nodes (
 id text PRIMARY KEY,
 project_key text NOT NULL,
 type text NOT NULL,
 name text NOT NULL,
 props jsonb NOT NULL DEFAULT '{}'::jsonb,
 object_ids text[] NOT NULL DEFAULT '{}',
 document_ids text[] NOT NULL DEFAULT '{}',
 search_text text NOT NULL DEFAULT '',
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS kg_nodes_project_type ON aec.kg_nodes(project_key, type);
CREATE INDEX IF NOT EXISTS kg_nodes_trgm ON aec.kg_nodes USING gin(search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS kg_nodes_documents ON aec.kg_nodes USING gin(document_ids);
CREATE INDEX IF NOT EXISTS kg_nodes_objects ON aec.kg_nodes USING gin(object_ids);

CREATE TABLE IF NOT EXISTS aec.kg_edges (
 src text NOT NULL REFERENCES aec.kg_nodes(id) ON DELETE CASCADE,
 predicate text NOT NULL,
 dst text NOT NULL REFERENCES aec.kg_nodes(id) ON DELETE CASCADE,
 project_key text NOT NULL,
 weight real NOT NULL DEFAULT 1,
 evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
 PRIMARY KEY(src, predicate, dst)
);
CREATE INDEX IF NOT EXISTS kg_edges_dst ON aec.kg_edges(dst, predicate);
CREATE INDEX IF NOT EXISTS kg_edges_project ON aec.kg_edges(project_key);

-- Entity resolution: every surface form (folder project id, room alias, section spelling, sheet number)
-- that was merged into one canonical node.
CREATE TABLE IF NOT EXISTS aec.kg_aliases (
 alias_type text NOT NULL,
 alias text NOT NULL,
 node_id text NOT NULL REFERENCES aec.kg_nodes(id) ON DELETE CASCADE,
 PRIMARY KEY(alias_type, alias, node_id)
);
CREATE INDEX IF NOT EXISTS kg_aliases_node ON aec.kg_aliases(node_id);

-- One row per canonical project: what the last build saw, so a rebuild only touches changed projects.
CREATE TABLE IF NOT EXISTS aec.kg_build_state (
 project_key text PRIMARY KEY,
 fingerprint text NOT NULL,
 nodes integer NOT NULL DEFAULT 0,
 edges integer NOT NULL DEFAULT 0,
 built_at timestamptz NOT NULL DEFAULT now()
);

-- Community / project summaries written by the local LLM. input_hash makes the run resumable and
-- cached: a community whose member facts did not change is never summarised again.
CREATE TABLE IF NOT EXISTS aec.kg_communities (
 id text PRIMARY KEY,
 project_key text NOT NULL,
 level integer NOT NULL,
 title text NOT NULL DEFAULT '',
 node_ids text[] NOT NULL DEFAULT '{}',
 facts jsonb NOT NULL DEFAULT '[]'::jsonb,
 input_hash text NOT NULL,
 summary text,
 model text,
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','DONE','FAILED')),
 error text,
 embedding vector(1024),
 embedding_model text,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS kg_communities_project ON aec.kg_communities(project_key, level);
