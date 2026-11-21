"""SQLite schema, connection helpers and transaction boundaries.

Every mutation runs inside ``BEGIN IMMEDIATE``.  SQLite then grants the write
lock to exactly one transaction, which is what makes "uploads and seals take
effect in commit order" true, and what lets a reversal + re-booking be
committed together or not at all.

The old-warehouse takeover adds two tables (``takeovers`` and
``takeover_tickets``) and two extra booking kinds (``takeover_book`` and
``takeover_deduct``).  Databases created by the first schema version are
migrated in place by :func:`ensure_schema`; the migration is one short,
fully serialised DDL block guarded by ``PRAGMA user_version``.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

from .errors import BusyError

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_no        TEXT PRIMARY KEY,
    collection_month TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    street           TEXT NOT NULL,
    weight_grams     INTEGER NOT NULL CHECK (weight_grams >= 0),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    filename       TEXT NOT NULL,
    pending_month  TEXT NOT NULL,
    uploaded_at    TEXT NOT NULL,
    row_count      INTEGER NOT NULL,
    new_count      INTEGER NOT NULL,
    corrected_count INTEGER NOT NULL,
    noop_count     INTEGER NOT NULL,
    digest         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS takeovers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    filename            TEXT NOT NULL,
    submitted_at        TEXT NOT NULL,
    row_count           INTEGER NOT NULL,
    register_only_count INTEGER NOT NULL,
    register_won_count  INTEGER NOT NULL,
    new_won_count       INTEGER NOT NULL,
    digest              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    first_pending_month TEXT NOT NULL,
    current_pending_month TEXT NOT NULL,
    takeover_mode       INTEGER NOT NULL DEFAULT 0 CHECK (takeover_mode IN (0, 1)),
    takeover_status     TEXT NOT NULL DEFAULT 'none'
                            CHECK (takeover_status IN ('none', 'pending', 'done')),
    takeover_id         INTEGER REFERENCES takeovers(id),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS takeover_tickets (
    ticket_no        TEXT PRIMARY KEY,
    takeover_id      INTEGER NOT NULL REFERENCES takeovers(id),
    collection_month TEXT NOT NULL,
    register_revision INTEGER NOT NULL,
    register_street  TEXT NOT NULL,
    register_grams   INTEGER NOT NULL,
    effective_revision INTEGER NOT NULL,
    effective_source TEXT NOT NULL
                         CHECK (effective_source IN ('register', 'new')),
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         INTEGER REFERENCES batches(id),
    takeover_id      INTEGER REFERENCES takeovers(id),
    ticket_no        TEXT NOT NULL REFERENCES tickets(ticket_no),
    revision         INTEGER NOT NULL,
    target_month     TEXT NOT NULL,
    collection_month TEXT NOT NULL,
    street           TEXT NOT NULL,
    grams            INTEGER NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN (
                         'book', 'reverse', 'takeover_book', 'takeover_deduct'
                     )),
    created_at       TEXT NOT NULL,
    CHECK (batch_id IS NOT NULL OR takeover_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_bookings_target
    ON bookings (target_month, street);
CREATE INDEX IF NOT EXISTS idx_bookings_ticket
    ON bookings (ticket_no, id);

CREATE TABLE IF NOT EXISTS snapshots (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    month                  TEXT NOT NULL UNIQUE,
    seq                    INTEGER NOT NULL UNIQUE,
    sealed_at              TEXT NOT NULL,
    total_current_grams    INTEGER NOT NULL,
    total_adjustment_grams INTEGER NOT NULL,
    total_grams            INTEGER NOT NULL,
    payload_json           TEXT NOT NULL
);
"""


def resolve_db_path(data_dir: str | None = None) -> str:
    explicit = os.environ.get("LEDGER_DB")
    if explicit:
        return explicit
    base = data_dir or os.environ.get("DATA_DIR") or "data"
    return os.path.join(base, "ledger.db")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def ensure_schema(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        # Created here rather than inside SCHEMA: legacy databases rebuilt
        # their bookings table during migration, and the column only exists
        # once that rebuild has run.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bookings_takeover "
            "ON bookings (takeover_id)"
        )
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade a database created before the takeover feature.

    The original schema (``user_version = 0``) has no takeover columns and a
    bookings CHECK that only knows ``book`` / ``reverse``.  SQLite cannot alter
    a CHECK in place, so the bookings table is rebuilt once; every row is
    copied verbatim and both old indexes are recreated.  The whole migration
    runs before the server accepts traffic and is idempotent via
    ``user_version``.
    """

    version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    if version >= SCHEMA_VERSION:
        return

    state_cols = {row["name"] for row in conn.execute("PRAGMA table_info(state)")}
    if "takeover_mode" not in state_cols:
        conn.execute(
            "ALTER TABLE state ADD COLUMN takeover_mode INTEGER NOT NULL "
            "DEFAULT 0 CHECK (takeover_mode IN (0, 1))"
        )
    if "takeover_status" not in state_cols:
        conn.execute(
            "ALTER TABLE state ADD COLUMN takeover_status TEXT NOT NULL "
            "DEFAULT 'none' CHECK (takeover_status IN ('none', 'pending', 'done'))"
        )
    if "takeover_id" not in state_cols:
        conn.execute("ALTER TABLE state ADD COLUMN takeover_id INTEGER")

    booking_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(bookings)")
    }
    if "takeover_id" not in booking_cols:
        # Rebuild so the wider kind CHECK and the nullable batch_id take effect.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE bookings_new (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id         INTEGER REFERENCES batches(id),
                    takeover_id      INTEGER REFERENCES takeovers(id),
                    ticket_no        TEXT NOT NULL REFERENCES tickets(ticket_no),
                    revision         INTEGER NOT NULL,
                    target_month     TEXT NOT NULL,
                    collection_month TEXT NOT NULL,
                    street           TEXT NOT NULL,
                    grams            INTEGER NOT NULL,
                    kind             TEXT NOT NULL CHECK (kind IN (
                                         'book', 'reverse',
                                         'takeover_book', 'takeover_deduct'
                                     )),
                    created_at       TEXT NOT NULL,
                    CHECK (batch_id IS NOT NULL OR takeover_id IS NOT NULL)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO bookings_new
                    (id, batch_id, takeover_id, ticket_no, revision, target_month,
                     collection_month, street, grams, kind, created_at)
                SELECT id, batch_id, NULL, ticket_no, revision, target_month,
                       collection_month, street, grams, kind, created_at
                  FROM bookings
                """
            )
            conn.execute("DROP TABLE bookings")
            conn.execute("ALTER TABLE bookings_new RENAME TO bookings")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bookings_target "
                "ON bookings (target_month, street)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bookings_ticket "
                "ON bookings (ticket_no, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bookings_takeover "
                "ON bookings (takeover_id)"
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


@contextmanager
def write_tx(db_path: str):
    """Serialised write transaction; rolls back on any exception."""

    conn = connect(db_path)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise BusyError("数据库正忙，本次操作未写入任何数据，请稍后重试") from exc
            raise
        try:
            yield conn
        except sqlite3.OperationalError as exc:
            _rollback_quietly(conn)
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise BusyError("数据库正忙，本次操作未写入任何数据，请稍后重试") from exc
            raise
        except BaseException:
            _rollback_quietly(conn)
            raise
        else:
            try:
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                _rollback_quietly(conn)
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise BusyError("数据库正忙，本次操作未提交") from exc
                raise
    finally:
        conn.close()


@contextmanager
def read_tx(db_path: str):
    """Consistent read snapshot across several SELECTs (WAL)."""

    conn = connect(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        try:
            conn.execute("COMMIT")
        except sqlite3.Error:
            pass
        conn.close()
