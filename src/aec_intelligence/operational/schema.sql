CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE SCHEMA IF NOT EXISTS aec;
CREATE TABLE IF NOT EXISTS aec.documents (
 id text PRIMARY KEY, project_id text NOT NULL, source_key text NOT NULL,
 name text NOT NULL, revision integer NOT NULL DEFAULT 0, source_hash text,
 snapshot_path text, updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(project_id, source_key)
);
CREATE INDEX IF NOT EXISTS documents_project ON aec.documents(project_id);
CREATE TABLE IF NOT EXISTS aec.snapshots (
 document_id text NOT NULL REFERENCES aec.documents(id), revision integer NOT NULL,
 snapshot_path text NOT NULL, source_hash text NOT NULL, created_at timestamptz DEFAULT now(),
 PRIMARY KEY(document_id, revision)
);
CREATE TABLE IF NOT EXISTS aec.objects (
 id text PRIMARY KEY, project_id text NOT NULL, document_id text NOT NULL REFERENCES aec.documents(id),
 revision integer NOT NULL, kind text NOT NULL, discipline text NOT NULL DEFAULT '', storey text NOT NULL DEFAULT '',
 label text NOT NULL, search_text text NOT NULL, payload jsonb NOT NULL,
 bounds geometry(Polygon,0), units text NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS objects_document ON aec.objects(document_id);
CREATE INDEX IF NOT EXISTS objects_filter ON aec.objects(project_id,discipline,storey,revision);
CREATE INDEX IF NOT EXISTS objects_trgm ON aec.objects USING gin(search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS objects_space ON aec.objects USING gist(bounds);
CREATE TABLE IF NOT EXISTS aec.embeddings (
 object_id text NOT NULL REFERENCES aec.objects(id) ON DELETE CASCADE,
 model text NOT NULL, revision integer NOT NULL, content_hash text NOT NULL,
 embedding vector(1024) NOT NULL, PRIMARY KEY(object_id,model)
);
CREATE INDEX IF NOT EXISTS embeddings_hnsw ON aec.embeddings USING hnsw(embedding vector_cosine_ops);
CREATE TABLE IF NOT EXISTS aec.relations (
 id text PRIMARY KEY, project_id text NOT NULL, document_id text NOT NULL REFERENCES aec.documents(id),
 revision integer NOT NULL, subject text NOT NULL, predicate text NOT NULL, object text NOT NULL,
 state text NOT NULL, evidence jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS relations_subject ON aec.relations(project_id,subject);
CREATE INDEX IF NOT EXISTS relations_object ON aec.relations(project_id,object);
CREATE TABLE IF NOT EXISTS aec.index_state (
 document_id text PRIMARY KEY REFERENCES aec.documents(id),
 sql_revision integer NOT NULL, graph_revision integer NOT NULL DEFAULT 0,
 embedding_revision integer NOT NULL DEFAULT 0, embedding_model text,
 rag_revision integer NOT NULL DEFAULT 0, error text
);
CREATE TABLE IF NOT EXISTS aec.jobs (
 id uuid PRIMARY KEY, dedup_key text NOT NULL UNIQUE, payload jsonb NOT NULL,
 state text NOT NULL DEFAULT 'QUEUED' CHECK(state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')),
 stage text NOT NULL DEFAULT 'queued', attempts integer NOT NULL DEFAULT 0,
 lease_owner text, lease_until timestamptz, progress integer NOT NULL DEFAULT 0,
 result jsonb, error text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_queue ON aec.jobs(state,created_at);
CREATE TABLE IF NOT EXISTS aec.metrics (
 id bigserial PRIMARY KEY, kind text NOT NULL, payload jsonb NOT NULL, created_at timestamptz DEFAULT now()
);
