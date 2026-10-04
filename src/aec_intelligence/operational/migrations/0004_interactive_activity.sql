-- Interactive priority (see operational/priority.py): the API stamps the time of its last query here and
-- background writers (reembed, the ingest workers' embedding step) pause their chunks while the stamp is
-- recent. UNLOGGED: the row is a hint, not data; it writes no WAL and may be empty after a crash.
CREATE UNLOGGED TABLE IF NOT EXISTS aec.interactive_activity (
 source text PRIMARY KEY,
 last_at timestamptz NOT NULL DEFAULT now()
);
