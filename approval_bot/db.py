from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS verification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    result TEXT NOT NULL,
    method TEXT,
    input_value TEXT,
    referrer_id INTEGER,
    reason TEXT,
    actor_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_user ON verification_events (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_result ON verification_events (result, created_at);

CREATE TABLE IF NOT EXISTS attempt_state (
    user_id INTEGER PRIMARY KEY,
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
"""


class Result:
    APPROVED = "approved"
    MANUAL_APPROVED = "manual_approved"
    DECLINED = "declined"
    LOCKED = "locked"
    RESET = "reset"
    ERROR = "error"


# Filters offered by /approvals list, mapped to the results they include.
RESULT_FILTERS: dict[str, tuple[str, ...]] = {
    "approved": (Result.APPROVED, Result.MANUAL_APPROVED),
    "declined": (Result.DECLINED,),
    "locked": (Result.LOCKED,),
}


@dataclass(slots=True, frozen=True)
class AttemptState:
    failed_count: int = 0
    locked: bool = False


@dataclass(slots=True, frozen=True)
class Event:
    id: int
    created_at: int
    user_id: int
    username: str
    result: str
    method: str | None
    input_value: str | None
    referrer_id: int | None
    reason: str | None
    actor_id: int | None


class ApprovalDatabase:
    """SQLite store for the verification audit log and per-user attempt counters."""

    def __init__(self, path: Path):
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    async def connect(self) -> None:
        await asyncio.to_thread(self._connect)

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    def _connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        self._conn = conn

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        def locked() -> T:
            with self._lock:
                return fn(*args)

        return await asyncio.to_thread(locked)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    # --- events -------------------------------------------------------------

    async def log_event(
        self,
        *,
        user_id: int,
        username: str,
        result: str,
        method: str | None = None,
        input_value: str | None = None,
        referrer_id: int | None = None,
        reason: str | None = None,
        actor_id: int | None = None,
    ) -> None:
        def insert() -> None:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO verification_events "
                    "(created_at, user_id, username, result, method, input_value, referrer_id, reason, actor_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (int(time.time()), user_id, username, result, method, input_value, referrer_id, reason, actor_id),
                )

        await self._run(insert)

    async def list_events(
        self,
        *,
        results: tuple[str, ...] | None = None,
        user_id: int | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        def select() -> list[Event]:
            clauses: list[str] = []
            params: list[Any] = []
            if results:
                clauses.append(f"result IN ({', '.join('?' * len(results))})")
                params.extend(results)
            if user_id is not None:
                clauses.append("user_id = ?")
                params.append(user_id)
            sql = "SELECT * FROM verification_events"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at DESC, id DESC"
            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)
            return [Event(**dict(row)) for row in self.conn.execute(sql, params)]

        return await self._run(select)

    async def count_by_result(self) -> dict[str, int]:
        def count() -> dict[str, int]:
            rows = self.conn.execute("SELECT result, COUNT(*) FROM verification_events GROUP BY result")
            return {result: total for result, total in rows}

        return await self._run(count)

    # --- attempts -----------------------------------------------------------

    def _get_attempts(self, user_id: int) -> AttemptState:
        row = self.conn.execute(
            "SELECT failed_count, locked FROM attempt_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        return AttemptState(row["failed_count"], bool(row["locked"])) if row else AttemptState()

    async def get_attempts(self, user_id: int) -> AttemptState:
        return await self._run(self._get_attempts, user_id)

    async def record_failure(self, user_id: int, max_attempts: int) -> AttemptState:
        """Count one failed attempt and lock the user once they reach max_attempts."""

        def update() -> AttemptState:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO attempt_state (user_id, failed_count, locked, updated_at) VALUES (?, 1, 0, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET "
                    "failed_count = failed_count + 1, updated_at = excluded.updated_at",
                    (user_id, int(time.time())),
                )
                self.conn.execute(
                    "UPDATE attempt_state SET locked = 1 WHERE user_id = ? AND failed_count >= ?",
                    (user_id, max_attempts),
                )
            return self._get_attempts(user_id)

        return await self._run(update)

    async def reset_attempts(self, user_id: int) -> None:
        def delete() -> None:
            with self.conn:
                self.conn.execute("DELETE FROM attempt_state WHERE user_id = ?", (user_id,))

        await self._run(delete)
