from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from webhooks_mcp.constants import (
    EVENT_TTL,
    LIST_NAMES_LIMIT,
    MAX_EVENTS_PER_NAME,
    MAX_STORE_BYTES,
    MINTED_PREFIX,
    ROW_OVERHEAD_BYTES,
    WAIT_RESULT_LIMIT,
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_BASE32 = "abcdefghijklmnopqrstuvwxyz234567"
_COLUMNS = "seq, event_id, event_name, payload, payload_bytes, received_at, source_ip"
_INLINE_COLUMNS = (
    "seq, event_id, event_name, CASE WHEN payload_bytes <= ? THEN payload END,"
    " payload_bytes, received_at, source_ip"
)
_META_COLUMNS = "seq, event_id, event_name, payload_bytes, received_at, source_ip"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_name TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_bytes INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    source_ip TEXT,
    idempotency_key TEXT,
    UNIQUE (event_name, idempotency_key)
);
CREATE INDEX IF NOT EXISTS events_name_seq ON events (event_name, seq);
CREATE INDEX IF NOT EXISTS events_received ON events (received_at);
CREATE TABLE IF NOT EXISTS minted_urls (
    event_name TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def is_valid_event_name(name: str) -> bool:
    return _NAME_RE.fullmatch(name) is not None


def _iso(moment: datetime) -> str:
    # Fixed width so stored timestamps compare correctly as text.
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Event:
    seq: int
    event_id: str
    event_name: str
    payload: Any
    payload_bytes: int
    received_at: str
    source_ip: str | None


@dataclass(frozen=True)
class Minted:
    event_name: str
    label: str
    expires_at: str


def _event(row: tuple) -> Event:
    seq, event_id, name, payload, size, received_at, source_ip = row
    parsed = None if payload is None else json.loads(payload)
    return Event(seq, event_id, name, parsed, size, received_at, source_ip)


def _sum_bytes(db: sqlite3.Connection) -> int:
    return db.execute(
        "SELECT COALESCE(SUM(payload_bytes), 0) + ? * COUNT(*) FROM events", (ROW_OVERHEAD_BYTES,)
    ).fetchone()[0]


def _trim_name(db: sqlite3.Connection, event_name: str) -> int:
    over = db.execute(
        "SELECT seq, payload_bytes FROM events WHERE event_name = ?"
        " ORDER BY seq DESC LIMIT -1 OFFSET ?",
        (event_name, MAX_EVENTS_PER_NAME),
    ).fetchall()
    if not over:
        return 0
    db.execute(
        "DELETE FROM events WHERE event_name = ? AND seq <= ?",
        (event_name, max(s for s, _ in over)),
    )
    return sum(size + ROW_OVERHEAD_BYTES for _, size in over)


def _evict_oldest(db: sqlite3.Connection, keep_seq: int, excess: int) -> int:
    if excess <= 0:
        return 0
    freed, last = 0, None
    cur = db.execute(
        "SELECT seq, payload_bytes FROM events WHERE seq < ? ORDER BY seq", (keep_seq,)
    )
    for seq, size in cur:
        freed, last = freed + size + ROW_OVERHEAD_BYTES, seq
        if freed >= excess:
            break
    cur.close()
    if last is not None:
        db.execute("DELETE FROM events WHERE seq <= ?", (last,))
    return freed


class EventStore:
    def __init__(self, path: Path, clock: Callable[[], datetime] = utcnow) -> None:
        self._path = path
        self._clock = clock
        self._db: sqlite3.Connection | None = None
        # Held by the worker thread, so a cancelled caller cannot release it mid-statement.
        self._write_lock = threading.Lock()
        self._cond = asyncio.Condition()
        self._inflight: set[asyncio.Future] = set()
        # Sum over events of payload_bytes + ROW_OVERHEAD_BYTES; touched only under the write lock.
        self._total_bytes = 0

    async def open(self) -> None:
        def _open() -> sqlite3.Connection:
            db = sqlite3.connect(self._path, check_same_thread=False)
            try:
                # Only takes effect before the first table exists; an existing file keeps its mode.
                db.execute("PRAGMA auto_vacuum = INCREMENTAL")
                mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(mode).lower() != "wal":
                    raise sqlite3.OperationalError(f"could not enable wal mode (got {mode})")
                db.execute("PRAGMA temp_store = MEMORY")  # read-only root fs: no temp files
                db.executescript(_SCHEMA)
                db.commit()
                self._total_bytes = _sum_bytes(db)
            except BaseException:
                db.close()
                raise
            return db

        self._db = await asyncio.to_thread(_open)

    async def close(self) -> None:
        # Shielded inserts outlive cancelled callers; let them finish before closing.
        while self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        if self._db is not None:
            db, self._db = self._db, None

            def _close() -> None:
                with self._write_lock:
                    db.close()

            await asyncio.to_thread(_close)

    async def _run[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        db = self._db
        if db is None:
            raise RuntimeError("store is not open")

        def _locked() -> T:
            with self._write_lock:
                return fn(db)

        return await asyncio.to_thread(_locked)

    async def insert(
        self,
        event_name: str,
        payload_json: str,
        source_ip: str | None,
        idempotency_key: str | None,
    ) -> tuple[Event, bool]:
        """Store an event; the returned Event (new or the original duplicate) has payload None."""
        event_id = str(uuid.uuid4())
        received_at = _iso(self._clock())
        size = len(payload_json.encode("utf-8"))

        def _insert(db: sqlite3.Connection) -> tuple[Event, bool]:
            with db:
                event, duplicate, delta = _insert_row(db)
            # Applied only after commit, so a rolled-back insert leaves the total untouched.
            self._total_bytes += delta
            return event, duplicate

        def _insert_row(db: sqlite3.Connection) -> tuple[Event, bool, int]:
            if idempotency_key is not None:
                row = db.execute(
                    f"SELECT {_META_COLUMNS} FROM events"
                    " WHERE event_name = ? AND idempotency_key = ?",
                    (event_name, idempotency_key),
                ).fetchone()
                if row is not None:
                    return Event(*row[:3], None, *row[3:]), True, 0
            cur = db.execute(
                "INSERT INTO events (event_id, event_name, payload, payload_bytes, received_at,"
                " source_ip, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, event_name, payload_json, size, received_at, source_ip, idempotency_key),
            )
            seq = cur.lastrowid
            delta = size + ROW_OVERHEAD_BYTES - _trim_name(db, event_name)
            delta -= _evict_oldest(db, seq, self._total_bytes + delta - MAX_STORE_BYTES)
            event = Event(seq, event_id, event_name, None, size, received_at, source_ip)
            return event, False, delta

        async def _insert_and_notify() -> tuple[Event, bool]:
            result = await self._run(_insert)
            if not result[1]:
                async with self._cond:
                    self._cond.notify_all()
            return result

        # Shielded so a cancelled caller cannot skip the wake-up of waiters.
        task = asyncio.ensure_future(_insert_and_notify())
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return await asyncio.shield(task)

    async def get(self, event_id: str) -> Event | None:
        def _get(db: sqlite3.Connection) -> Event | None:
            row = db.execute(
                f"SELECT {_COLUMNS} FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            return _event(row) if row else None

        return await self._run(_get)

    async def list(
        self, event_name: str, after_seq: int, limit: int, *, inline_max_bytes: int | None = None
    ) -> list[Event]:
        """With inline_max_bytes, payloads larger than it are not read and come back as None."""
        if inline_max_bytes is None:
            columns, params = _COLUMNS, (event_name, after_seq, limit)
        else:
            columns, params = _INLINE_COLUMNS, (inline_max_bytes, event_name, after_seq, limit)

        def _list(db: sqlite3.Connection) -> list[Event]:
            rows = db.execute(
                f"SELECT {columns} FROM events WHERE event_name = ? AND seq > ?"
                " ORDER BY seq ASC LIMIT ?",
                params,
            ).fetchall()
            return [_event(r) for r in rows]

        return await self._run(_list)

    async def wait(
        self,
        event_name: str,
        after_seq: int,
        timeout_s: float,
        *,
        inline_max_bytes: int | None = None,
    ) -> list[Event]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        async with self._cond:
            while True:
                found = await self.list(
                    event_name, after_seq, WAIT_RESULT_LIMIT, inline_max_bytes=inline_max_bytes
                )
                if found:
                    return found
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return []
                try:
                    await asyncio.wait_for(self._cond.wait(), remaining)
                except TimeoutError:
                    return []

    async def names(self) -> tuple[list[tuple[str, int]], bool]:
        """At most LIST_NAMES_LIMIT (name, count) pairs by name, and whether more exist."""

        def _names(db: sqlite3.Connection) -> tuple[list[tuple[str, int]], bool]:
            rows = db.execute(
                "SELECT event_name, COUNT(*) FROM events GROUP BY event_name ORDER BY event_name"
                " LIMIT ?",
                (LIST_NAMES_LIMIT + 1,),
            ).fetchall()
            return [(n, c) for n, c in rows[:LIST_NAMES_LIMIT]], len(rows) > LIST_NAMES_LIMIT

        return await self._run(_names)

    async def mint(self, label: str) -> Minted:
        suffix = "".join(secrets.choice(_BASE32) for _ in range(12))
        name = f"{MINTED_PREFIX}{label}-{suffix}"
        if not is_valid_event_name(name):
            raise ValueError("label does not form a valid event name")
        now = self._clock()
        expires_at = _iso(now + EVENT_TTL)

        def _mint(db: sqlite3.Connection) -> None:
            with db:
                db.execute(
                    "INSERT INTO minted_urls (event_name, label, created_at, expires_at)"
                    " VALUES (?, ?, ?, ?)",
                    (name, label, _iso(now), expires_at),
                )

        await self._run(_mint)
        return Minted(name, label, expires_at)

    async def is_minted_active(self, event_name: str) -> bool:
        now = _iso(self._clock())

        def _active(db: sqlite3.Connection) -> bool:
            row = db.execute(
                "SELECT 1 FROM minted_urls WHERE event_name = ? AND expires_at >= ?",
                (event_name, now),
            ).fetchone()
            return row is not None

        return await self._run(_active)

    async def sweep(self) -> None:
        moment = self._clock()
        cutoff = _iso(moment - EVENT_TTL)
        now = _iso(moment)

        def _sweep(db: sqlite3.Connection) -> None:
            with db:
                db.execute("DELETE FROM events WHERE received_at < ?", (cutoff,))
                db.execute("DELETE FROM minted_urls WHERE expires_at < ?", (now,))
                db.execute(
                    "DELETE FROM events WHERE seq IN ("
                    " SELECT seq FROM ("
                    "  SELECT seq, ROW_NUMBER() OVER (PARTITION BY event_name ORDER BY seq DESC)"
                    "  AS rn FROM events) WHERE rn > ?)",
                    (MAX_EVENTS_PER_NAME,),
                )
                total = _sum_bytes(db)
            self._total_bytes = total
            db.execute("PRAGMA incremental_vacuum").fetchall()

        await self._run(_sweep)
