"""SQLite-backed session persistence for WhatsApp conversation state."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import time
from typing import Any, Dict, Optional

SESSION_DB_PATH = os.getenv("SESSION_DB_PATH", "session_store.db")


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or SESSION_DB_PATH
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


@contextmanager
def _db_connection(db_path: Optional[str] = None):
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_session_store(db_path: Optional[str] = None) -> None:
    with _db_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                phone TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()


def save_session(phone: str, payload: Dict[str, Any], db_path: Optional[str] = None) -> None:
    now = time.time()
    raw_payload = json.dumps(payload, ensure_ascii=False)
    with _db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sessions(phone, payload, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(phone)
            DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (phone, raw_payload, now),
        )
        conn.commit()


def load_session(phone: str, ttl_seconds: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with _db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT payload, updated_at FROM sessions WHERE phone = ?",
            (phone,),
        ).fetchone()

        if not row:
            return None

        payload_raw, updated_at = row
        updated_at = float(updated_at)

        if ttl_seconds > 0 and (time.time() - updated_at) > ttl_seconds:
            conn.execute("DELETE FROM sessions WHERE phone = ?", (phone,))
            conn.commit()
            return None

        try:
            payload = json.loads(payload_raw)
        except Exception:
            conn.execute("DELETE FROM sessions WHERE phone = ?", (phone,))
            conn.commit()
            return None

        if not isinstance(payload, dict):
            conn.execute("DELETE FROM sessions WHERE phone = ?", (phone,))
            conn.commit()
            return None

        payload.setdefault("ts", updated_at)
        return payload


def delete_session(phone: str, db_path: Optional[str] = None) -> None:
    with _db_connection(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE phone = ?", (phone,))
        conn.commit()


def cleanup_expired_sessions(ttl_seconds: int, db_path: Optional[str] = None) -> int:
    if ttl_seconds <= 0:
        return 0

    cutoff = time.time() - ttl_seconds
    with _db_connection(db_path) as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount or 0
