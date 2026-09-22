"""SQLite connection management, schema bootstrap, FTS helpers (PRD §9, §17.3).

Connection model (per the architecture decision): a single long-lived *writer*
connection owned by the indexer background task, and short-lived *reader*
connections opened per API request. WAL permits one writer concurrently with
many readers, so reads never block on the write lock. Readers are opened with
``PRAGMA query_only=ON`` for defence in depth.

``reset_derived`` deliberately preserves ``notes``/``folders``/``tags`` so a full
rebuild upserts into them (and marks unseen rows deleted), keeping ``notes.rowid``
stable — the external-content ``notes_fts`` index is keyed on rowid, so deleting
and reinserting notes would corrupt it.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from pkm_sidecar.errors import DatabaseLockedError, SchemaVersionMismatchError

SCHEMA_VERSION = 2  # v2 adds resources + note_resources (delete & resync to upgrade)
SCHEMA_VERSION_KEY = "schema.version"

# Well-known meta keys (PRD §9).
META_LAST_EVENT_ID = "joplin.last_event_id"
META_LAST_FULL_INDEX_AT = "joplin.last_full_index_at"
META_LAST_INCREMENTAL_INDEX_AT = "joplin.last_incremental_index_at"
# Set in the same transaction that wipes the derived tables and cleared in the
# one that stamps META_LAST_FULL_INDEX_AT, so its presence means exactly "the
# derived tables were emptied and not yet refilled". A rebuild that is killed or
# fails in between leaves it behind, which is how startup knows to redo the work
# instead of serving a half-built index (see services.rebuild_reason).
META_REBUILD_IN_PROGRESS = "joplin.rebuild_in_progress"

_LOCK_RETRY_DELAYS = (0.05, 0.2, 0.5)


def _load_schema_sql() -> str:
    return files("pkm_sidecar").joinpath("schema.sql").read_text(encoding="utf-8")


def _configure(conn: sqlite3.Connection, *, read_only: bool) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    # Manage transactions explicitly (BEGIN IMMEDIATE in transaction()).
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    if read_only:
        conn.execute("PRAGMA query_only=ON")
    return conn


def open_writer_connection(path: str | Path) -> sqlite3.Connection:
    """Open the single writer connection (owned by the indexer)."""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return _configure(conn, read_only=False)


def open_reader_connection(path: str | Path) -> sqlite3.Connection:
    """Open a short-lived read-only connection (one per API request)."""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return _configure(conn, read_only=True)


def init_db(path: str | Path) -> None:
    """Create the data directory and apply the schema; safe to call repeatedly."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = open_writer_connection(path)
    try:
        conn.executescript(_load_schema_sql())
        existing = get_meta(conn, SCHEMA_VERSION_KEY)
        if existing is None:
            set_meta(conn, SCHEMA_VERSION_KEY, str(SCHEMA_VERSION))
        elif int(existing) != SCHEMA_VERSION:
            raise SchemaVersionMismatchError(
                f"On-disk schema version {existing} != supported {SCHEMA_VERSION}. "
                "Delete the index database to rebuild."
            )
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE … COMMIT/ROLLBACK with bounded retry on a locked database.

    Retries the BEGIN up to three times (50/200/500 ms); raises
    :class:`DatabaseLockedError` if the lock never clears (PRD §17.3).
    """
    attempt = 0
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                if attempt < len(_LOCK_RETRY_DELAYS):
                    time.sleep(_LOCK_RETRY_DELAYS[attempt])
                    attempt += 1
                    continue
                raise DatabaseLockedError(str(exc)) from exc
            raise
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def fts_escape(query: str) -> str:
    """Turn arbitrary user input into a safe FTS5 MATCH expression.

    Each whitespace-separated token becomes a double-quoted phrase (embedded
    quotes doubled), joined with spaces (implicit AND). This prevents the user
    from injecting FTS5 operators. Returns ``""`` for empty/whitespace input.
    """
    tokens = query.split()
    quoted = [f'"{tok.replace(chr(34), chr(34) * 2)}"' for tok in tokens if tok]
    return " ".join(quoted)


def fts_available(conn: sqlite3.Connection) -> bool:
    """Return True if this SQLite build supports FTS5 (PRD §15.5 doctor check)."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__fts_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.__fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def reset_derived(conn: sqlite3.Connection, *, started_at: int) -> None:
    """Clear derived/link tables before a full rebuild, preserving the entities.

    Wipes ``extracted_tasks``, ``extracted_links``, ``note_tags`` and the event
    cursor / incremental timestamp. ``notes``/``folders``/``tags`` are kept so the
    rebuild upserts them (preserving ``notes.rowid`` for FTS); orphaned rows are
    marked deleted afterwards by ``sweep_orphans``.

    Raises the :data:`META_REBUILD_IN_PROGRESS` flag (to *started_at*) in the same
    transaction as the wipe, so the "derived data is incomplete" marker can never
    be missing while the tables are empty. :func:`clear_rebuild_in_progress`
    lowers it once the rebuild finishes.
    """
    with transaction(conn):
        set_meta(conn, META_REBUILD_IN_PROGRESS, str(started_at))
        conn.execute("DELETE FROM extracted_tasks")
        conn.execute("DELETE FROM extracted_links")
        conn.execute("DELETE FROM note_tags")
        conn.execute("DELETE FROM note_resources")
        conn.execute(
            "DELETE FROM meta WHERE key IN (?, ?)",
            (
                META_LAST_EVENT_ID,
                META_LAST_INCREMENTAL_INDEX_AT,
            ),
        )


def clear_rebuild_in_progress(conn: sqlite3.Connection) -> None:
    """Lower the incomplete-index flag. Caller owns the transaction."""
    conn.execute("DELETE FROM meta WHERE key = ?", (META_REBUILD_IN_PROGRESS,))


def rebuild_in_progress_since(conn: sqlite3.Connection) -> int | None:
    """When the unfinished rebuild started (epoch seconds), or None if there is none."""
    raw = get_meta(conn, META_REBUILD_IN_PROGRESS)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:  # hand-edited or corrupt: still means "incomplete"
        return 0
