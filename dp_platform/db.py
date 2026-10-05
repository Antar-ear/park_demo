"""SQLite storage: users, batches, drawings. Stdlib only."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

import drawing_common as dc

from . import config

# The extracted-field columns come straight from dc.FIELD_ORDER so the schema
# can't drift from the extraction engine's own field list -- adding a field
# there is enough for it to show up here too (see _migrate() for existing DBs).
_FIELD_COLS_DDL = ",\n    ".join("%s TEXT NOT NULL DEFAULT ''" % f for f in dc.FIELD_ORDER)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drawings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',      -- queued | processing | done
    status TEXT,                               -- DEFENSE | NON_DEFENSE | NEEDS_REVIEW | FAILED
    reason TEXT,
    %s,
    method TEXT,
    seconds REAL,
    full_text TEXT NOT NULL DEFAULT '',
    warnings TEXT NOT NULL DEFAULT '',
    edited_fields TEXT NOT NULL DEFAULT '[]',
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_drawings_batch ON drawings(batch_id);
CREATE INDEX IF NOT EXISTS idx_drawings_state ON drawings(state);
""" % _FIELD_COLS_DDL

# Columns the browser may see. full_text is deliberately left out: it is large and
# only the server needs it (to re-classify after an edit).
PUBLIC_COLS = ("id, batch_id, filename, state, status, reason, "
              + ", ".join(dc.FIELD_ORDER)
              + ", method, seconds, warnings, edited_fields, processed_at")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """One short-lived connection per operation; safe across the web and worker threads."""
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(c: sqlite3.Connection) -> None:
    """
    Add any FIELD_ORDER columns missing from an existing `drawings` table.

    `CREATE TABLE IF NOT EXISTS` only covers a brand-new database -- it does
    nothing for a database created before a field was added to FIELD_ORDER.
    This keeps an older `./data` folder working after a schema change instead
    of requiring it to be deleted.
    """
    existing = {row["name"] for row in c.execute("PRAGMA table_info(drawings)")}
    for name in dc.FIELD_ORDER:
        if name not in existing:
            c.execute("ALTER TABLE drawings ADD COLUMN %s TEXT NOT NULL DEFAULT ''" % name)


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        _migrate(c)


def _row(r: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if r is None:
        return None
    d = dict(r)
    if "edited_fields" in d:
        d["edited_fields"] = json.loads(d["edited_fields"] or "[]")
    return d


# --- users --------------------------------------------------------------------------
def get_user_by_name(username: str) -> Optional[Dict[str, Any]]:
    with connect() as c:
        return _row(c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    with connect() as c:
        return _row(c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def create_user(username: str, password_hash: str) -> int:
    with connect() as c:
        return c.execute("INSERT INTO users(username, password_hash, created_at) VALUES (?,?,?)",
                         (username, password_hash, now())).lastrowid


# --- batches ------------------------------------------------------------------------
def create_batch(user_id: int, name: str) -> int:
    with connect() as c:
        return c.execute("INSERT INTO batches(user_id, name, created_at) VALUES (?,?,?)",
                         (user_id, name, now())).lastrowid


def list_batches(user_id: int) -> List[Dict[str, Any]]:
    with connect() as c:
        rows = c.execute("""
            SELECT b.id, b.name, b.created_at,
                   COUNT(d.id) AS total,
                   SUM(d.state = 'done') AS done,
                   SUM(d.status = 'DEFENSE') AS defense,
                   SUM(d.status = 'NON_DEFENSE') AS non_defense,
                   SUM(d.status = 'NEEDS_REVIEW') AS needs_review,
                   SUM(d.status = 'FAILED') AS failed
            FROM batches b LEFT JOIN drawings d ON d.batch_id = b.id
            WHERE b.user_id = ? GROUP BY b.id ORDER BY b.id DESC""", (user_id,)).fetchall()
    return [{k: (v or 0) if k not in ("name", "created_at") else v
             for k, v in dict(r).items()} for r in rows]


def get_batch(batch_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    """The batch, only if it belongs to user_id."""
    with connect() as c:
        return _row(c.execute("SELECT * FROM batches WHERE id = ? AND user_id = ?",
                              (batch_id, user_id)).fetchone())


# --- drawings -----------------------------------------------------------------------
def add_drawing(batch_id: int, filename: str, stored_path: str) -> int:
    with connect() as c:
        return c.execute("INSERT INTO drawings(batch_id, filename, stored_path) VALUES (?,?,?)",
                         (batch_id, filename, stored_path)).lastrowid


def batch_drawings(batch_id: int) -> List[Dict[str, Any]]:
    with connect() as c:
        rows = c.execute("SELECT %s FROM drawings WHERE batch_id = ? ORDER BY id" % PUBLIC_COLS,
                         (batch_id,)).fetchall()
    return [_row(r) for r in rows]


def get_drawing_for_user(drawing_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    """Full drawing row (including stored_path and full_text), only if the user owns it."""
    with connect() as c:
        return _row(c.execute("""SELECT d.* FROM drawings d JOIN batches b ON b.id = d.batch_id
                                 WHERE d.id = ? AND b.user_id = ?""",
                              (drawing_id, user_id)).fetchone())


def get_drawing(drawing_id: int) -> Optional[Dict[str, Any]]:
    with connect() as c:
        return _row(c.execute("SELECT * FROM drawings WHERE id = ?", (drawing_id,)).fetchone())


def unfinished_ids() -> List[int]:
    """Drawings interrupted by a restart; the worker picks them up again."""
    with connect() as c:
        c.execute("UPDATE drawings SET state = 'queued' WHERE state = 'processing'")
        return [r["id"] for r in c.execute(
            "SELECT id FROM drawings WHERE state = 'queued' ORDER BY id").fetchall()]


def mark_processing(drawing_id: int) -> None:
    with connect() as c:
        c.execute("UPDATE drawings SET state = 'processing' WHERE id = ?", (drawing_id,))


def save_result(drawing_id: int, r: Dict[str, Any]) -> None:
    field_cols = ", ".join("%s = ?" % f for f in dc.FIELD_ORDER)
    with connect() as c:
        c.execute("""UPDATE drawings SET state = 'done', status = ?, reason = ?,
                     %s, method = ?, seconds = ?, full_text = ?, warnings = ?,
                     processed_at = ? WHERE id = ?""" % field_cols,
                  (r["status"], r["reason"], *(r[f] for f in dc.FIELD_ORDER),
                   r["method"], r["seconds"], r["full_text"], r["warnings"],
                   now(), drawing_id))


def save_edit(drawing_id: int, fields: Dict[str, str], status: str, reason: str,
              edited: List[str]) -> None:
    field_cols = ", ".join("%s = ?" % f for f in dc.FIELD_ORDER)
    with connect() as c:
        c.execute("""UPDATE drawings SET %s, status = ?, reason = ?, edited_fields = ?
                     WHERE id = ?""" % field_cols,
                  (*(fields[f] for f in dc.FIELD_ORDER), status, reason,
                   json.dumps(edited), drawing_id))
