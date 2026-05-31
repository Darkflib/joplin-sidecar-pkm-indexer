-- pkm-sidecar SQLite schema (PRD §9). Idempotent: safe to run on every startup.
-- Packaged as data and loaded via importlib.resources. PRAGMAs (WAL, foreign_keys,
-- busy_timeout, synchronous) are applied per-connection in db.py, not here.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
    id           TEXT PRIMARY KEY,
    parent_id    TEXT,
    title        TEXT NOT NULL,
    updated_time INTEGER,
    created_time INTEGER,
    indexed_at   INTEGER NOT NULL,
    deleted      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notes (
    id                TEXT PRIMARY KEY,
    parent_id         TEXT,
    title             TEXT NOT NULL,
    body              TEXT NOT NULL,
    body_hash         TEXT NOT NULL,
    created_time      INTEGER,
    updated_time      INTEGER,
    user_created_time INTEGER,
    user_updated_time INTEGER,
    is_todo           INTEGER NOT NULL DEFAULT 0,
    todo_due          INTEGER,
    todo_completed    INTEGER,
    source_url        TEXT,
    indexed_at        INTEGER NOT NULL,
    deleted           INTEGER NOT NULL DEFAULT 0
    -- No FK on parent_id: Joplin returns notes whose parent folder may not be in
    -- /folders (Trash/Conflicts, fetch races). This is a derived index that only
    -- soft-deletes, so referential enforcement would just spuriously fail inserts.
    -- parent_id is recorded as-is; folder views LEFT/INNER JOIN and tolerate gaps.
);

CREATE TABLE IF NOT EXISTS tags (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    created_time INTEGER,
    updated_time INTEGER,
    indexed_at   INTEGER NOT NULL,
    deleted      INTEGER NOT NULL DEFAULT 0
);

-- No FKs: a tag's note list (from /tags/{id}/notes) can reference a note absent
-- from our index (Trash/Conflicts). mark_tag_deleted prunes rows explicitly, and
-- views JOIN to notes(deleted=0), so orphan rows are harmless.
CREATE TABLE IF NOT EXISTS note_tags (
    note_id    TEXT NOT NULL,
    tag_id     TEXT NOT NULL,
    indexed_at INTEGER NOT NULL,
    PRIMARY KEY (note_id, tag_id)
);

CREATE TABLE IF NOT EXISTS extracted_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id     TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    checked     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    raw_line    TEXT NOT NULL,
    indexed_at  INTEGER NOT NULL,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS extracted_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id     TEXT NOT NULL,
    line_number INTEGER,
    link_text   TEXT,
    target      TEXT NOT NULL,
    link_type   TEXT NOT NULL,
    indexed_at  INTEGER NOT NULL,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);

-- Resource (attachment) metadata only — no blob download/OCR (still out of scope).
CREATE TABLE IF NOT EXISTS resources (
    id             TEXT PRIMARY KEY,
    title          TEXT,
    mime           TEXT,
    filename       TEXT,
    file_extension TEXT,
    size           INTEGER,
    created_time   INTEGER,
    updated_time   INTEGER,
    indexed_at     INTEGER NOT NULL,
    deleted        INTEGER NOT NULL DEFAULT 0
);

-- Derived from extracted_links ∩ resources (a note embeds a resource as :/<id>).
CREATE TABLE IF NOT EXISTS note_resources (
    note_id     TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    indexed_at  INTEGER NOT NULL,
    PRIMARY KEY (note_id, resource_id)
);

CREATE TABLE IF NOT EXISTS index_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    INTEGER NOT NULL,
    completed_at  INTEGER,
    mode          TEXT NOT NULL,
    status        TEXT NOT NULL,
    notes_seen    INTEGER NOT NULL DEFAULT 0,
    notes_updated INTEGER NOT NULL DEFAULT 0,
    folders_seen  INTEGER NOT NULL DEFAULT 0,
    tags_seen     INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    message       TEXT
);

-- Secondary indexes for the workflow views and joins.
CREATE INDEX IF NOT EXISTS idx_notes_parent_id    ON notes(parent_id);
CREATE INDEX IF NOT EXISTS idx_notes_updated_time ON notes(updated_time);
CREATE INDEX IF NOT EXISTS idx_notes_is_todo      ON notes(is_todo);
CREATE INDEX IF NOT EXISTS idx_notes_deleted      ON notes(deleted);
CREATE INDEX IF NOT EXISTS idx_note_tags_tag_id   ON note_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_tasks_note_id      ON extracted_tasks(note_id);
CREATE INDEX IF NOT EXISTS idx_links_note_id      ON extracted_links(note_id);
CREATE INDEX IF NOT EXISTS idx_links_target       ON extracted_links(target);
CREATE INDEX IF NOT EXISTS idx_folders_parent_id  ON folders(parent_id);
CREATE INDEX IF NOT EXISTS idx_note_resources_rid ON note_resources(resource_id);

-- Full-text search over title + body, external-content table keyed on notes.rowid.
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title,
    body,
    content='notes',
    content_rowid='rowid'
);

-- Keep notes_fts in sync. UPDATE/DELETE use the special 'delete' command with
-- the OLD values so the external-content index stays consistent.
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;

CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body)
    VALUES ('delete', old.rowid, old.title, old.body);
END;

CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body)
    VALUES ('delete', old.rowid, old.title, old.body);
    INSERT INTO notes_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;
