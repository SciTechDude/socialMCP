"""SQLite connection management, migrations, and the sqlite-vec attachment.

Everything is local: one file on disk, WAL mode, foreign keys on. The vector
index is optional by design — if sqlite-vec is not installed the database still
works and search degrades to FTS5 + structured filters rather than failing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import struct
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ..config import Config

log = logging.getLogger("socialmcp.db")

SCHEMA_VERSION = 2
_SCHEMA_SQL = Path(__file__).with_name("schema.sql")
_SCHEMA_VEC_SQL = Path(__file__).with_name("schema_vec.sql")


class Database:
    """Thin wrapper over sqlite3 with the bits every caller needs.

    Not thread-safe on purpose: the MCP server is single-process and async, and
    all writes funnel through the ingest pipeline. `check_same_thread=False` is
    set so the watchdog observer thread can hand work to the same connection
    under the module-level write lock held by the ingest pipeline.
    """

    def __init__(self, path: Path | str, *, embedding_dim: int = 768, read_only: bool = False):
        self.path = Path(path)
        self.embedding_dim = embedding_dim
        self.read_only = read_only
        self.vec_enabled = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._configure()
        self._load_vec_extension()
        if not read_only:
            self.migrate()

    # -- setup ---------------------------------------------------------------

    def _configure(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA foreign_keys = ON")
        cur.execute("PRAGMA synchronous = NORMAL")
        cur.execute("PRAGMA busy_timeout = 5000")
        cur.close()

    def _load_vec_extension(self) -> None:
        try:
            import sqlite_vec
        except ImportError:
            log.info("sqlite-vec not installed; semantic search disabled (keyword search still works)")
            return
        try:
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.vec_enabled = True
        except (AttributeError, sqlite3.OperationalError) as exc:
            # Python built without extension support, or a platform mismatch.
            log.warning("sqlite-vec present but could not load (%s); semantic search disabled", exc)
        finally:
            with contextlib.suppress(AttributeError):
                self.conn.enable_load_extension(False)

    def migrate(self) -> None:
        """Apply the schema. Idempotent — safe on every startup."""
        self.conn.executescript(_SCHEMA_SQL.read_text())
        self._add_missing_columns()
        self._rebuild_views()

        current = self.get_meta("schema_version")
        if current is None or int(current) < SCHEMA_VERSION:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif int(current) > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database at {self.path} was written by a newer SocialMCP "
                f"(schema_version {current} > {SCHEMA_VERSION}). Upgrade the package."
            )

        if self.vec_enabled:
            self._migrate_vec()
        self.conn.commit()

    # Column additions are applied with ALTER TABLE rather than by rebuilding
    # the table, so an existing database upgrades in place without touching the
    # user's data. (A CHECK-constraint change would require a rebuild, which is
    # why lapsed invitations are recorded in their own column instead of as a
    # new `status` value.)
    ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("contacts", "invite_lapsed_at", "TEXT"),
    )

    def _add_missing_columns(self) -> None:
        for table, column, decl in self.ADDED_COLUMNS:
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                log.info("adding column %s.%s", table, column)
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _rebuild_views(self) -> None:
        """CREATE VIEW IF NOT EXISTS never updates a changed definition."""
        for name in ("v_pending_invitations", "v_contact_clusters"):
            self.conn.execute(f"DROP VIEW IF EXISTS {name}")
        self.conn.executescript(
            _SCHEMA_SQL.read_text().split("-- Convenience views")[-1]
        )

    def _migrate_vec(self) -> None:
        stored_dim = self.get_meta("embedding_dim")
        if stored_dim is not None and int(stored_dim) != self.embedding_dim:
            # Switching embedding models invalidates every stored vector.
            log.warning(
                "embedding dimension changed %s -> %s; rebuilding vec_contacts",
                stored_dim,
                self.embedding_dim,
            )
            self.conn.execute("DROP TABLE IF EXISTS vec_contacts")
            self.conn.execute("UPDATE contacts SET embedding_dirty = 1, embedding_hash = NULL")
        ddl = _SCHEMA_VEC_SQL.read_text().replace("{DIM}", str(self.embedding_dim))
        self.conn.executescript(ddl)
        self.set_meta("embedding_dim", str(self.embedding_dim))

    # -- primitives ----------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        return self.conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        row = self.query_one(sql, params)
        return row[0] if row else None

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- meta ----------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        return self.scalar("SELECT value FROM meta WHERE key = ?", (key,))

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- vectors -------------------------------------------------------------

    def upsert_embedding(self, contact_id: int, vector: Sequence[float]) -> None:
        if not self.vec_enabled:
            return
        if len(vector) != self.embedding_dim:
            raise ValueError(f"expected {self.embedding_dim}-dim vector, got {len(vector)}")
        blob = serialize_f32(vector)
        self.conn.execute("DELETE FROM vec_contacts WHERE contact_id = ?", (contact_id,))
        self.conn.execute(
            "INSERT INTO vec_contacts(contact_id, embedding) VALUES (?, ?)", (contact_id, blob)
        )

    # -- stats ---------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        row = self.query_one(
            """
            SELECT
              (SELECT COUNT(*) FROM contacts)                                AS contacts,
              (SELECT COUNT(*) FROM contacts WHERE status = 'connected')     AS connected,
              (SELECT COUNT(*) FROM contacts WHERE status = 'pending')       AS pending_invites,
              (SELECT COUNT(*) FROM interactions)                            AS interactions,
              (SELECT COUNT(*) FROM interactions WHERE kind = 'note')        AS notes,
              (SELECT COUNT(*) FROM event_clusters)                          AS event_clusters,
              (SELECT COUNT(*) FROM contact_history)                         AS history_changes,
              (SELECT COUNT(*) FROM archives)                                AS archives_ingested,
              (SELECT MAX(ingested_at) FROM archives)                        AS last_ingest_at,
              (SELECT COUNT(*) FROM contacts WHERE embedding_dirty = 1)      AS embeddings_pending
            """
        )
        out = dict(row) if row else {}
        out["db_path"] = str(self.path)
        out["vector_search"] = self.vec_enabled
        out["sources"] = [
            dict(r)
            for r in self.query(
                "SELECT source, COUNT(*) AS contacts FROM contacts GROUP BY source ORDER BY 2 DESC"
            )
        ]
        return out


def serialize_f32(vector: Sequence[float]) -> bytes:
    """Pack a float list into the little-endian f32 blob sqlite-vec expects."""
    return struct.pack(f"<{len(vector)}f", *vector)


def deserialize_f32(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def open_database(config: Config) -> Database:
    config.ensure_dirs()
    return Database(config.db_path, embedding_dim=config.embedding.dim)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
