"""Connection management and schema bootstrap for the enrichment store.

A second SQLite file beside the index (``suggestions.sqlite3``). It carries the
accept/reject decisions, which are *not* derivable and must survive both
``index rebuild`` and the index's delete-and-resync upgrade path.

Connection handling mirrors :mod:`pkm_sidecar.db` — WAL, a bounded busy timeout,
explicit transactions — and reuses its ``transaction`` helper so the locked-database
retry policy has one implementation. The version check here deliberately runs
*before* the schema is applied, unlike the index's, so a mismatched database is
never partially upgraded on the way to being refused.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

from pkm_sidecar.db import get_meta, set_meta, transaction
from pkm_sidecar.errors import SchemaVersionMismatchError

SCHEMA_VERSION = 2  # v2: note_embeddings keys on input_hash, not body_hash
SCHEMA_VERSION_KEY = "schema.version"

DEFAULT_FILENAME = "suggestions.sqlite3"


def _load_schema_sql() -> str:
    return files("pkm_sidecar.enrichment").joinpath("schema.sql").read_text(encoding="utf-8")


def default_path(index_path: Path) -> Path:
    """Where the store lives: beside the index, so one data directory holds both."""
    return index_path.parent / DEFAULT_FILENAME


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v2 rekeys ``note_embeddings`` from ``body_hash`` to ``input_hash``.

    Dropped rather than rewritten: the table is *derived*, and refilling it costs
    a few minutes of embedding. What must not be touched is ``suggestions`` and
    ``opt_outs``, which hold accept/reject decisions that are not derivable from
    anything — protecting those is the entire reason this is a separate database
    from the index. Forcing a whole-file delete to change a derived table would
    destroy exactly what that separation exists to keep.
    """
    with transaction(conn):
        conn.execute("DROP TABLE IF EXISTS note_embeddings")
        set_meta(conn, SCHEMA_VERSION_KEY, "2")


# from-version -> migration. A version with no entry here is genuinely
# incompatible and is refused rather than guessed at.
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {1: _migrate_v1_to_v2}


def _configure(conn: sqlite3.Connection, *, read_only: bool) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # explicit BEGIN IMMEDIATE via db.transaction
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    if read_only:
        conn.execute("PRAGMA query_only=ON")
    return conn


def open_connection(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return _configure(conn, read_only=read_only)


def _existing_version(conn: sqlite3.Connection) -> str | None:
    """Read the stored version, tolerating a database that predates the meta table."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if row is None:
        return None
    return get_meta(conn, SCHEMA_VERSION_KEY)


def init_db(path: str | Path) -> None:
    """Create the store, migrating it forward where possible; safe to repeat.

    Checks the on-disk version *before* touching the schema, so an unrecognised
    database is refused untouched rather than having this version's tables grafted
    onto it on the way out.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = open_connection(path)
    try:
        existing = _existing_version(conn)
        if existing is not None and existing != str(SCHEMA_VERSION):
            try:
                from_version = int(existing)
            except ValueError:
                from_version = -1
            migration = _MIGRATIONS.get(from_version)
            if migration is None:
                raise SchemaVersionMismatchError(
                    f"Enrichment store at {path} is schema version {existing}, and this "
                    f"build supports {SCHEMA_VERSION} with no migration from it. Delete "
                    "the file to rebuild — suggestions regenerate, but accept/reject "
                    "decisions are lost."
                )
            migration(conn)
        # Recreates anything a migration dropped; idempotent otherwise.
        conn.executescript(_load_schema_sql())
        if existing is None:
            set_meta(conn, SCHEMA_VERSION_KEY, str(SCHEMA_VERSION))
    finally:
        conn.close()
