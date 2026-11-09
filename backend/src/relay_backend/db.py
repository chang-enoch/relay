"""SQLite access: one connection, numbered SQL migrations, and a transaction helper
that appends replay events and publishes them only after commit."""

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version in applied:
            continue
        conn.execute("BEGIN")
        try:
            for statement in path.read_text().split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def purge_expired(conn: sqlite3.Connection, days: int) -> int:
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    cur = conn.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))
    return cur.rowcount


class Tx:
    """An open transaction. emit() records a replay event; events are published after commit."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.events: list[dict] = []

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def next_sequence(self, session_id: str) -> int:
        row = self.one(
            "UPDATE sessions SET event_seq = event_seq + 1 WHERE id = ? RETURNING event_seq",
            (session_id,),
        )
        if row is None:
            raise LookupError("session not found")
        return row[0]

    def emit(self, session_id: str, type_: str, payload: dict, visibility: str = "shared") -> dict:
        seq = self.next_sequence(session_id)
        event = {
            "id": new_id("evt"),
            "sequence": seq,
            "timestamp": now(),
            "type": type_,
            "payload": payload,
            "visibility": visibility,
        }
        self.execute(
            "INSERT INTO events (id, session_id, sequence, timestamp, type, payload, visibility)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event["id"], session_id, seq, event["timestamp"], type_, dumps(payload), visibility),
        )
        event["session_id"] = session_id
        return self._record(event)

    def _record(self, event: dict) -> dict:
        self.events.append(event)
        return event


class Database:
    def __init__(self, path: str, publish: Callable[[list[dict]], None] | None = None):
        self.conn = connect(path)
        self.publish = publish or (lambda events: None)

    @contextmanager
    def tx(self) -> Iterator[Tx]:
        """BEGIN IMMEDIATE ... COMMIT; publish emitted events only once committed."""
        tx = Tx(self.conn)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield tx
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        if tx.events:
            self.publish(tx.events)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()


def row_dict(row: sqlite3.Row | None, json_fields: tuple[str, ...] = ()) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for f in json_fields:
        if d.get(f) is not None:
            d[f] = json.loads(d[f])
    return d
