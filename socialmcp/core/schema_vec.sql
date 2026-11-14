-- Vector index, applied only when the sqlite-vec extension loads successfully.
-- {DIM} is substituted at runtime with config.embedding_dim, because changing
-- the embedding model changes the vector width and vec0 needs it at DDL time.
--
-- Dropping and rebuilding this table is always safe: contacts.embedding_dirty
-- is reset to 1 by core/db.py whenever the stored dimension changes, so the
-- next ingest (or `socialmcp reindex`) refills it.

CREATE VIRTUAL TABLE IF NOT EXISTS vec_contacts USING vec0(
    contact_id INTEGER PRIMARY KEY,
    embedding  FLOAT[{DIM}]
);
