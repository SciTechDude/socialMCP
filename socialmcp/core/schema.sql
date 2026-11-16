-- SocialMCP core schema (schema_version 1)
--
-- Design rules:
--   * Every table is source-agnostic. Adapters (linkedin, instagram, x, vcard...)
--     write the SAME shapes; `source` + `source_key` is the identity pair.
--   * All timestamps are stored as TEXT in ISO-8601 UTC ('YYYY-MM-DDTHH:MM:SSZ'),
--     all plain dates as TEXT 'YYYY-MM-DD'. This keeps SQLite date functions and
--     lexicographic range filters working together.
--   * FTS5 is an external-content index over `contacts`, kept in sync by triggers.
--   * The vector table (`vec_contacts`) is created separately at runtime from
--     schema_vec.sql, because it needs the sqlite-vec extension to be loadable
--     and the embedding dimension is config-dependent. See core/db.py.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- meta / migrations
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ---------------------------------------------------------------------------
-- archives: the ingest ledger. One row per .zip (or directory) ever ingested.
-- sha256 is UNIQUE so re-dropping the same export is a cheap no-op.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS archives (
    id          INTEGER PRIMARY KEY,
    source      TEXT    NOT NULL,              -- 'linkedin'
    file_name   TEXT    NOT NULL,
    file_path   TEXT,                          -- last known path on disk
    sha256      TEXT    NOT NULL UNIQUE,
    size_bytes  INTEGER,
    exported_at TEXT,                          -- best-guess export date from archive
    ingested_at TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'ok'  -- 'ok' | 'partial' | 'failed'
                CHECK (status IN ('ok', 'partial', 'failed')),
    stats_json  TEXT                           -- {"contacts_new":…, "files":[…], "errors":[…]}
);

-- ---------------------------------------------------------------------------
-- contacts: one row per human, per source.
-- The FTS5 index mirrors (full_name, company, position, headline, origin_note,
-- search_blob) so those column names must not change without updating triggers.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    source_key      TEXT NOT NULL,             -- 'in/jane-doe' or 'name/jane-doe|acme'
    profile_url     TEXT,                      -- normalized canonical URL
    full_name       TEXT NOT NULL,
    first_name      TEXT,
    last_name       TEXT,
    headline        TEXT,                      -- free-text bio/tagline when export has one
    company         TEXT,
    "position"      TEXT,
    location        TEXT,
    email           TEXT,

    -- Lifecycle. 'pending' = we invited them and they are not in Connections yet.
    -- 'received'  = they invited us and we have not accepted yet.
    status          TEXT NOT NULL DEFAULT 'connected'
                    CHECK (status IN ('connected', 'pending', 'received', 'withdrawn', 'unknown')),

    -- The clipboard/venue context that makes this whole project worth building.
    origin_note     TEXT,
    origin_direction TEXT CHECK (origin_direction IN ('outbound', 'inbound')),

    invited_at          TEXT,                  -- ISO-8601 UTC, from Invitations.csv
    -- Set when an invitation that was pending in an earlier archive has
    -- vanished from a later one without becoming a connection: declined,
    -- withdrawn, ignored or expired. LinkedIn's export does not say which, so
    -- neither do we. Only inferred while the invite is still inside the
    -- export's ~6-month invitation window; outside it, absence is meaningless.
    invite_lapsed_at    TEXT,
    connected_on        TEXT,                  -- 'YYYY-MM-DD', from Connections.csv
    last_interaction_at TEXT,                  -- max(interactions.occurred_at)

    -- Denormalized text bucket for keyword search: notes + message snippets.
    search_blob     TEXT NOT NULL DEFAULT '',

    -- Embedding bookkeeping: `embedding_hash` is the hash of the text last
    -- embedded, so re-ingest only re-embeds contacts whose text actually moved.
    embedding_hash  TEXT,
    embedding_dirty INTEGER NOT NULL DEFAULT 1 CHECK (embedding_dirty IN (0, 1)),

    first_archive_id INTEGER REFERENCES archives(id) ON DELETE SET NULL,
    last_archive_id  INTEGER REFERENCES archives(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,

    UNIQUE (source, source_key)
);

CREATE INDEX IF NOT EXISTS idx_contacts_connected_on  ON contacts (connected_on);
CREATE INDEX IF NOT EXISTS idx_contacts_status        ON contacts (status);
CREATE INDEX IF NOT EXISTS idx_contacts_company       ON contacts (company);
CREATE INDEX IF NOT EXISTS idx_contacts_profile_url   ON contacts (profile_url);
CREATE INDEX IF NOT EXISTS idx_contacts_dirty         ON contacts (embedding_dirty) WHERE embedding_dirty = 1;

-- ---------------------------------------------------------------------------
-- contact_identities: alias table used for cross-file matching.
-- LinkedIn is inconsistent: Connections.csv has /in/<vanity-slug>, messages.csv
-- often has /in/ACoAAB… opaque member URNs, and some rows have no URL at all.
-- Adapters register every identity they see; resolution tries url -> email -> name.
-- ---------------------------------------------------------------------------
-- The uniqueness is per (identity, contact) rather than per identity: two real
-- people genuinely share a display name, and we want to SEE that ambiguity and
-- refuse to merge, not crash on a constraint or silently fuse two humans.
CREATE TABLE IF NOT EXISTS contact_identities (
    id         INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    source     TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('profile_url', 'email', 'name', 'handle', 'thread')),
    value      TEXT NOT NULL,
    UNIQUE (source, kind, value, contact_id)
);

CREATE INDEX IF NOT EXISTS idx_identities_contact ON contact_identities (contact_id);
CREATE INDEX IF NOT EXISTS idx_identities_lookup  ON contact_identities (source, kind, value);

-- ---------------------------------------------------------------------------
-- interactions: invitations, accepted connections, DMs, and user notes.
-- `dedupe_key` is adapter-chosen and stable across re-ingests of overlapping
-- archives (e.g. a monthly export that repeats last month's invitations).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS interactions (
    id          INTEGER PRIMARY KEY,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('invitation', 'connection', 'message', 'note')),
    direction   TEXT CHECK (direction IN ('outbound', 'inbound', 'self')),
    occurred_at TEXT,
    subject     TEXT,
    body        TEXT,
    thread_id   TEXT,
    dedupe_key  TEXT,
    archive_id  INTEGER REFERENCES archives(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (source, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_interactions_contact ON interactions (contact_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_interactions_kind    ON interactions (kind, occurred_at DESC);

-- ---------------------------------------------------------------------------
-- contact_history: job/company drift detected between archive drops.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contact_history (
    id         INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,                  -- 'company' | 'position' | 'headline' | …
    old_value  TEXT,
    new_value  TEXT,
    changed_at TEXT NOT NULL,                  -- when WE observed it (ingest time)
    archive_id INTEGER REFERENCES archives(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_history_contact ON contact_history (contact_id, changed_at DESC);

-- ---------------------------------------------------------------------------
-- event_clusters: deterministic temporal bursts ("Seattle AI Week").
-- Auto-created with a date-range name; renaming sets renamed_by_user=1 and the
-- clusterer then never overwrites the name again.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_clusters (
    id             INTEGER PRIMARY KEY,
    slug           TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    start_date     TEXT NOT NULL,              -- 'YYYY-MM-DD'
    end_date       TEXT NOT NULL,
    source         TEXT,
    contact_count  INTEGER NOT NULL DEFAULT 0,
    auto_generated INTEGER NOT NULL DEFAULT 1 CHECK (auto_generated IN (0, 1)),
    renamed_by_user INTEGER NOT NULL DEFAULT 0 CHECK (renamed_by_user IN (0, 1)),
    notes          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clusters_range ON event_clusters (start_date, end_date);

-- A contact can legitimately belong to more than one cluster (two events in the
-- same week), so this is a join table rather than a column on contacts.
CREATE TABLE IF NOT EXISTS contact_event_clusters (
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    cluster_id INTEGER NOT NULL REFERENCES event_clusters(id) ON DELETE CASCADE,
    PRIMARY KEY (contact_id, cluster_id)
);

CREATE INDEX IF NOT EXISTS idx_cec_cluster ON contact_event_clusters (cluster_id);

-- ---------------------------------------------------------------------------
-- harness_events: the local agent harness's activity log.
-- The harness runs autonomously (file watcher -> ingest -> cluster), so when a
-- chat session opens there may be work the user never saw. Unacknowledged rows
-- are what the briefing resource surfaces as "while you were away".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS harness_events (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,          -- 'ingest' | 'cluster' | 'error' | 'watch'
    level        TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info', 'warning', 'error')),
    summary      TEXT NOT NULL,          -- one human-readable line
    detail_json  TEXT,
    archive_id   INTEGER REFERENCES archives(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_events_unacked ON harness_events (acknowledged, created_at DESC);

-- ---------------------------------------------------------------------------
-- FTS5 keyword index (external content over `contacts`)
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS contacts_fts USING fts5(
    full_name,
    company,
    "position",
    headline,
    origin_note,
    search_blob,
    content='contacts',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS contacts_fts_ai AFTER INSERT ON contacts BEGIN
    INSERT INTO contacts_fts(rowid, full_name, company, "position", headline, origin_note, search_blob)
    VALUES (new.id, new.full_name, new.company, new."position", new.headline, new.origin_note, new.search_blob);
END;

CREATE TRIGGER IF NOT EXISTS contacts_fts_ad AFTER DELETE ON contacts BEGIN
    INSERT INTO contacts_fts(contacts_fts, rowid, full_name, company, "position", headline, origin_note, search_blob)
    VALUES ('delete', old.id, old.full_name, old.company, old."position", old.headline, old.origin_note, old.search_blob);
END;

CREATE TRIGGER IF NOT EXISTS contacts_fts_au AFTER UPDATE ON contacts BEGIN
    INSERT INTO contacts_fts(contacts_fts, rowid, full_name, company, "position", headline, origin_note, search_blob)
    VALUES ('delete', old.id, old.full_name, old.company, old."position", old.headline, old.origin_note, old.search_blob);
    INSERT INTO contacts_fts(rowid, full_name, company, "position", headline, origin_note, search_blob)
    VALUES (new.id, new.full_name, new.company, new."position", new.headline, new.origin_note, new.search_blob);
END;

-- ---------------------------------------------------------------------------
-- Convenience views
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_pending_invitations AS
SELECT c.id, c.full_name, c.company, c."position", c.profile_url,
       c.origin_note, c.invited_at,
       CAST(julianday('now') - julianday(c.invited_at) AS INTEGER) AS days_waiting
FROM contacts c
WHERE c.status = 'pending' AND c.invite_lapsed_at IS NULL
ORDER BY c.invited_at DESC;

CREATE VIEW IF NOT EXISTS v_contact_clusters AS
SELECT c.id AS contact_id, c.full_name, ec.id AS cluster_id, ec.slug, ec.name,
       ec.start_date, ec.end_date
FROM contacts c
JOIN contact_event_clusters cec ON cec.contact_id = c.id
JOIN event_clusters ec ON ec.id = cec.cluster_id;
