-- Enrichment store (docs/enrichment.md §4). A *separate* database from the
-- index: suggestions are derived and regenerable, but the accept/reject
-- decisions on them are not, and the index's documented upgrade path is
-- "delete the file". Non-derived state must not live somewhere disposable.
--
-- Idempotent: safe to run on every startup. PRAGMAs are per-connection, in db.py.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id           TEXT NOT NULL,
    kind              TEXT NOT NULL,                  -- 'title' | 'tags'
    -- Identity over every input that can change the answer: the body, the state
    -- being replaced (title, or tag set), the model and the prompt. Keying on
    -- notes.body_hash alone would miss a retitle, letting a stale rejection
    -- suppress a newly applicable suggestion.
    input_hash        TEXT NOT NULL,
    payload           TEXT NOT NULL,                  -- JSON
    current_value     TEXT,                           -- JSON; what it would replace
    note_body_hash    TEXT NOT NULL,                  -- component of input_hash, for diagnostics
    note_updated_time INTEGER,                        -- optimistic-concurrency token for the apply path
    corpus_revision   TEXT,                           -- tags only: drives staleness, NOT identity
    model             TEXT NOT NULL,
    prompt_version    INTEGER NOT NULL,
    confidence        REAL,
    reason            TEXT,                           -- why the note was a candidate
    stale             INTEGER NOT NULL DEFAULT 0,
    -- The corpus a tag suggestion depends on shifts whenever any note is tagged.
    -- Folding that into input_hash would mean accepting one suggestion
    -- invalidates every other pending one, so a drifted row is re-queued at the
    -- next generation and kept servable until its replacement lands. Both rows
    -- share an input_hash by construction, hence generation in the unique key.
    generation        INTEGER NOT NULL DEFAULT 1,
    superseded_by     INTEGER REFERENCES suggestions(id) ON DELETE SET NULL,
    decision          TEXT,                           -- NULL | 'accepted' | 'rejected'
    decided_at        INTEGER,
    created_at        INTEGER NOT NULL,
    UNIQUE(note_id, kind, input_hash, generation)
);

-- "Leave this note's title alone" — distinct from rejecting one suggestion, and
-- must survive regeneration, a body edit, and a model change.
CREATE TABLE IF NOT EXISTS opt_outs (
    note_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (note_id, kind)
);

-- Derived, but kept here so the index schema stays at v2 and existing users are
-- spared a delete-and-resync. Keyed by model as well as note: vectors from
-- different embedding models are not comparable, so a body-only lookup after an
-- embedding_model change would silently mix spaces and corrupt every
-- nearest-neighbour result. Reuse requires body_hash AND model to match.
CREATE TABLE IF NOT EXISTS note_embeddings (
    note_id    TEXT NOT NULL,
    model      TEXT NOT NULL,
    body_hash  TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (note_id, model)
);

CREATE INDEX IF NOT EXISTS idx_suggestions_note     ON suggestions(note_id, kind);
CREATE INDEX IF NOT EXISTS idx_suggestions_pending  ON suggestions(kind, decision, generation);
CREATE INDEX IF NOT EXISTS idx_suggestions_stale    ON suggestions(kind, stale);
