-- Storage (Phase 5): one half-precision vector per distinct text instead of one float vector per object.
--
-- Before: aec.embeddings held a vector(1024) (4 KB) per object plus an HNSW index over all of them
-- (~10 MB per drawing; 2.7 GB of a 4.5 GB database at 458 drawings). CAD text repeats heavily
-- ("1F", dimension values, room names, title-block labels on every sheet), and bge-m3 vectors keep
-- their ranking at fp16, so:
--   * aec.text_vectors   (model, content_hash) -> halfvec(1024): one row per distinct embedded text,
--                        HNSW (halfvec_cosine_ops) over these only;
--   * aec.embeddings     (object_id, model, revision, content_hash): which text vector an object uses.
-- content_hash is sha256(search_text) as before, so existing rows map 1:1. Writers insert the text
-- vector first (ON CONFLICT DO NOTHING) and the mapping second; the foreign key keeps a mapping from
-- pointing at a vector that `vectors-gc` removed concurrently.
SET LOCAL maintenance_work_mem = '512MB';
-- DISTINCT ON over every vector sorts ~1 GB at 300k objects; keep it mostly in memory.
SET LOCAL work_mem = '256MB';
-- Parallel index builds need /dev/shm, which is 64 MB in the default container.
SET LOCAL max_parallel_maintenance_workers = 0;

CREATE TABLE IF NOT EXISTS aec.text_vectors (
 model text NOT NULL,
 content_hash text NOT NULL,
 embedding halfvec(1024) NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (model, content_hash)
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'aec' AND table_name = 'embeddings' AND column_name = 'embedding') THEN
    INSERT INTO aec.text_vectors(model, content_hash, embedding)
    SELECT DISTINCT ON (model, content_hash) model, content_hash, embedding::halfvec(1024)
    FROM aec.embeddings
    ORDER BY model, content_hash, revision DESC
    ON CONFLICT DO NOTHING;

    -- Rebuild the mapping table without the vector column (DROP COLUMN would keep the bytes until
    -- a VACUUM FULL); dropping the old table frees its heap, TOAST and HNSW files at commit.
    CREATE TABLE aec.embeddings_v3 AS
      SELECT object_id, model, revision, content_hash FROM aec.embeddings;
    DROP TABLE aec.embeddings;
    ALTER TABLE aec.embeddings_v3 RENAME TO embeddings;
    ALTER TABLE aec.embeddings
      ALTER COLUMN object_id SET NOT NULL, ALTER COLUMN model SET NOT NULL,
      ALTER COLUMN revision SET NOT NULL, ALTER COLUMN content_hash SET NOT NULL,
      ADD PRIMARY KEY (object_id, model),
      ADD CONSTRAINT embeddings_object_id_fkey FOREIGN KEY (object_id) REFERENCES aec.objects(id) ON DELETE CASCADE;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'embeddings_text_vector_fkey') THEN
    ALTER TABLE aec.embeddings ADD CONSTRAINT embeddings_text_vector_fkey
      FOREIGN KEY (model, content_hash) REFERENCES aec.text_vectors(model, content_hash);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS embeddings_text ON aec.embeddings(model, content_hash);
CREATE INDEX IF NOT EXISTS text_vectors_hnsw ON aec.text_vectors USING hnsw (embedding halfvec_cosine_ops);
