"""SQLite helpers. One short-lived connection per unit of work; WAL so the API can read while a case writes."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from munim import config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or config.DB_PATH), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Connection inside a transaction: commit on success, rollback on error, always close."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def rows(conn: sqlite3.Connection, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def now_iso() -> str:
    """Wall-clock timestamp for workflow rows (cases, actions, audit). Business data uses config.DEMO_TODAY."""
    return datetime.now().isoformat(timespec="seconds")


def audit(conn: sqlite3.Connection, case_id: str | None, actor: str, event: str, payload: Any) -> None:
    conn.execute(
        "INSERT INTO audit_log(case_id, actor, event, payload_json, at) VALUES (?,?,?,?,?)",
        (case_id, actor, event, json.dumps(payload, default=str, ensure_ascii=False), now_iso()),
    )
