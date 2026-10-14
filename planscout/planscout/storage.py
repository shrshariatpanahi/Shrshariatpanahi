"""SQLite persistence for users, carrier connections, account snapshots and consent history."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .models import AccountSnapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connections (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    carrier_id TEXT NOT NULL,
    method TEXT NOT NULL,
    scopes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    token_ciphertext BLOB
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_pending (
    state TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    carrier_id TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Consent history outlives the connection on purpose: it records *that* access was granted
-- and revoked, without keeping any account data.
CREATE TABLE IF NOT EXISTS consent_events (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    carrier_id TEXT NOT NULL,
    method TEXT NOT NULL,
    event TEXT NOT NULL,
    scopes TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

PENDING_TTL = timedelta(minutes=10)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ users

    def create_user(self, email: str, password_hash: str) -> int:
        with self.connect() as db:
            cur = db.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, password_hash, _now()),
            )
            return cur.lastrowid

    def user_by_email(self, email: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    def user_by_id(self, user_id: int) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def delete_user(self, user_id: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    # ------------------------------------------------------------------ oauth state

    def save_pending(self, state: str, user_id: int, carrier_id: str, code_verifier: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO oauth_pending (state, user_id, carrier_id, code_verifier, created_at) VALUES (?, ?, ?, ?, ?)",
                (state, user_id, carrier_id, code_verifier, _now()),
            )

    def pop_pending(self, state: str, user_id: int) -> sqlite3.Row | None:
        """Consume a pending authorization; only valid for the user who started it, within the TTL."""
        cutoff = (datetime.now(timezone.utc) - PENDING_TTL).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute("DELETE FROM oauth_pending WHERE created_at < ?", (cutoff,))
            row = db.execute(
                "SELECT * FROM oauth_pending WHERE state = ? AND user_id = ?", (state, user_id)
            ).fetchone()
            if row:
                db.execute("DELETE FROM oauth_pending WHERE state = ?", (state,))
            return row

    # ------------------------------------------------------------------ connections

    def create_connection(
        self,
        user_id: int,
        carrier_id: str,
        method: str,
        scopes: list[str],
        snapshot: AccountSnapshot,
        token_ciphertext: bytes | None = None,
    ) -> int:
        now = _now()
        with self.connect() as db:
            cur = db.execute(
                "INSERT INTO connections (user_id, carrier_id, method, scopes, created_at, updated_at, token_ciphertext)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, carrier_id, method, json.dumps(scopes), now, now, token_ciphertext),
            )
            connection_id = cur.lastrowid
            db.execute(
                "INSERT INTO snapshots (connection_id, data, created_at) VALUES (?, ?, ?)",
                (connection_id, snapshot.model_dump_json(), now),
            )
            db.execute(
                "INSERT INTO consent_events (user_id, carrier_id, method, event, scopes, created_at)"
                " VALUES (?, ?, ?, 'granted', ?, ?)",
                (user_id, carrier_id, method, json.dumps(scopes), now),
            )
            return connection_id

    def add_snapshot(self, connection_id: int, snapshot: AccountSnapshot, token_ciphertext: bytes | None) -> None:
        now = _now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO snapshots (connection_id, data, created_at) VALUES (?, ?, ?)",
                (connection_id, snapshot.model_dump_json(), now),
            )
            db.execute(
                "UPDATE connections SET updated_at = ?, token_ciphertext = COALESCE(?, token_ciphertext) WHERE id = ?",
                (now, token_ciphertext, connection_id),
            )
            # Keep only the latest few snapshots: enough for history, not a long-term archive.
            db.execute(
                "DELETE FROM snapshots WHERE connection_id = ? AND id NOT IN "
                "(SELECT id FROM snapshots WHERE connection_id = ? ORDER BY id DESC LIMIT 3)",
                (connection_id, connection_id),
            )

    def connection(self, connection_id: int, user_id: int) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM connections WHERE id = ? AND user_id = ?", (connection_id, user_id)
            ).fetchone()

    def connections(self, user_id: int) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM connections WHERE user_id = ? ORDER BY id DESC", (user_id,)
            ).fetchall()

    def latest_snapshot(self, connection_id: int) -> AccountSnapshot | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT data FROM snapshots WHERE connection_id = ? ORDER BY id DESC LIMIT 1", (connection_id,)
            ).fetchone()
        return AccountSnapshot.model_validate_json(row["data"]) if row else None

    def delete_connection(self, connection_id: int, user_id: int) -> None:
        """Revoke: removes the stored token and every snapshot, keeps a consent record."""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM connections WHERE id = ? AND user_id = ?", (connection_id, user_id)
            ).fetchone()
            if not row:
                return
            db.execute("DELETE FROM connections WHERE id = ?", (connection_id,))
            db.execute(
                "INSERT INTO consent_events (user_id, carrier_id, method, event, scopes, created_at)"
                " VALUES (?, ?, ?, 'revoked', ?, ?)",
                (user_id, row["carrier_id"], row["method"], row["scopes"], _now()),
            )

    def consent_events(self, user_id: int) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM consent_events WHERE user_id = ? ORDER BY id DESC", (user_id,)
            ).fetchall()
