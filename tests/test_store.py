import asyncio
import os
import re
import sqlite3
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from webhooks_mcp.constants import EVENT_TTL, MAX_EVENTS_PER_NAME
from webhooks_mcp.store import EventStore, is_valid_event_name


class SlowInsertDb:
    """Delays event inserts so a test can cancel the caller while the worker thread runs."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args):
        if sql.startswith("INSERT INTO events"):
            time.sleep(0.2)
        return self._real.execute(sql, *args)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def test_insert_assigns_increasing_seq_and_uuid(store):
    a, dup_a = await store.insert("a.b", '{"x":1}', "100.64.0.1", None)
    b, _ = await store.insert("a.b", '{"x":2}', None, None)
    assert (dup_a, b.seq > a.seq, a.payload, a.payload_bytes) == (False, True, {"x": 1}, 7)
    assert uuid.UUID(a.event_id)


async def test_idempotency_key_returns_original(store):
    a, _ = await store.insert("a.b", "1", None, "k1")
    b, dup = await store.insert("a.b", "2", None, "k1")
    assert (b.event_id, dup, b.payload) == (a.event_id, True, 1)


async def test_same_key_under_another_name_is_a_new_event(store):
    a, _ = await store.insert("a.b", "1", None, "k1")
    b, dup = await store.insert("c.d", "1", None, "k1")
    assert (dup, a.event_id != b.event_id) == (False, True)


async def test_concurrent_same_key_makes_one_row(store):
    rs = await asyncio.gather(*[store.insert("a.b", "1", None, "k") for _ in range(20)])
    assert len({e.event_id for e, _ in rs}) == 1
    assert sum(1 for _, dup in rs if not dup) == 1


async def test_get_returns_event_or_none(store):
    a, _ = await store.insert("a.b", '{"x":1}', "100.64.0.1", None)
    assert await store.get(a.event_id) == a
    assert await store.get(str(uuid.uuid4())) is None


async def test_list_is_cursor_paginated(store):
    for i in range(5):
        await store.insert("a.b", str(i), None, None)
    first = await store.list("a.b", 0, 2)
    rest = await store.list("a.b", first[-1].seq, 10)
    assert [e.payload for e in first + rest] == [0, 1, 2, 3, 4]


async def test_names_counts_sorted_by_name(store):
    await store.insert("b.x", "1", None, None)
    await store.insert("a.x", "1", None, None)
    await store.insert("b.x", "2", None, None)
    assert await store.names() == [("a.x", 1), ("b.x", 2)]


async def test_wait_returns_immediately_when_events_exist(store):
    await store.insert("a.b", "1", None, None)
    start = time.monotonic()
    got = await store.wait("a.b", 0, 5)
    assert [e.payload for e in got] == [1]
    assert time.monotonic() - start < 0.1


async def test_wait_wakes_on_insert(store):
    async def later():
        await asyncio.sleep(0.05)
        await store.insert("a.b", "7", None, None)

    task = asyncio.create_task(later())
    start = time.monotonic()
    got = await store.wait("a.b", 0, 5)
    await task
    assert [e.payload for e in got] == [7]
    assert time.monotonic() - start < 1


async def test_wait_times_out_empty(store):
    assert await store.wait("a.b", 0, 0.05) == []


async def test_wait_ignores_other_names(store):
    async def later():
        await asyncio.sleep(0.02)
        await store.insert("x.y", "1", None, None)

    task = asyncio.create_task(later())
    assert await store.wait("a.b", 0, 0.2) == []
    await task


async def test_mint_name_shape_and_activity(store, clock):
    m = await store.mint("backfill")
    assert re.fullmatch(r"minted\.backfill-[a-z2-7]{12}", m.event_name)
    assert await store.is_minted_active(m.event_name)
    clock.advance(EVENT_TTL + timedelta(seconds=1))
    assert not await store.is_minted_active(m.event_name)


async def test_unknown_minted_name_is_inactive(store):
    assert not await store.is_minted_active("minted.nope-aaaaaaaaaaaa")


async def test_sweep_drops_expired_and_trims_per_name(store, clock):
    await store.insert("old.e", "1", None, None)
    clock.advance(EVENT_TTL + timedelta(seconds=1))
    for i in range(MAX_EVENTS_PER_NAME + 3):
        await store.insert("new.e", str(i), None, None)
    await store.sweep()
    assert await store.list("old.e", 0, 10) == []
    kept = await store.list("new.e", 0, MAX_EVENTS_PER_NAME + 10)
    assert (len(kept), kept[0].payload) == (MAX_EVENTS_PER_NAME, 3)


async def test_sweep_drops_expired_minted_urls(store, clock):
    m = await store.mint("x")
    clock.advance(EVENT_TTL + timedelta(seconds=1))
    await store.sweep()
    assert not await store.is_minted_active(m.event_name)


def test_event_name_validation():
    assert all(is_valid_event_name(n) for n in ["a", "ingest.backfill.completed", "a-b_c.1"])
    assert not any(is_valid_event_name(n) for n in ["", "A.b", ".a", "a/b", "a b", "a" * 129])


async def test_open_fails_when_parent_is_not_a_directory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises((sqlite3.OperationalError, OSError)):
        await EventStore(blocker / "events.db").open()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
async def test_open_fails_on_unwritable_path(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir(mode=0o500)
    with pytest.raises((sqlite3.OperationalError, OSError)):
        await EventStore(ro / "events.db").open()


async def test_payload_bytes_counts_utf8_bytes(store):
    e, _ = await store.insert("a.b", '"é€"', None, None)
    assert e.payload_bytes == 7


async def test_open_requires_wal(tmp_path):
    with pytest.raises(sqlite3.OperationalError, match="wal"):
        await EventStore(Path(":memory:")).open()


async def test_unopened_store_raises_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="not open"):
        await EventStore(tmp_path / "x.db").names()


@pytest.mark.parametrize("label", ["Bad Label", "a" * 200, "x/y"])
async def test_mint_rejects_label_that_makes_invalid_name(store, label):
    with pytest.raises(ValueError):
        await store.mint(label)


async def test_sweep_keeps_rows_exactly_at_ttl(store, clock):
    await store.insert("a.b", "1", None, None)
    m = await store.mint("x")
    clock.advance(EVENT_TTL)
    await store.sweep()
    assert len(await store.list("a.b", 0, 10)) == 1
    assert await store.is_minted_active(m.event_name)


async def test_cancelled_insert_does_not_break_single_writer(store):
    store._db = SlowInsertDb(store._db)
    first = asyncio.create_task(store.insert("a.b", "1", None, "k"))
    await asyncio.sleep(0.05)
    first.cancel()
    second, _ = await store.insert("a.b", "1", None, "k")
    assert len(await store.list("a.b", 0, 10)) == 1
    assert (await store.list("a.b", 0, 10))[0].event_id == second.event_id


async def test_waiter_woken_when_inserting_task_cancelled_after_commit(store):
    store._db = SlowInsertDb(store._db)
    waiter = asyncio.create_task(store.wait("a.b", 0, 5))
    await asyncio.sleep(0.02)
    inserter = asyncio.create_task(store.insert("a.b", "1", None, None))
    await asyncio.sleep(0.05)
    inserter.cancel()
    got = await asyncio.wait_for(waiter, 2)
    assert [e.payload for e in got] == [1]


async def test_failed_sweep_rolls_back_partial_deletes(store, clock, monkeypatch):
    await store.insert("old.e", "1", None, None)
    clock.advance(EVENT_TTL + timedelta(seconds=1))
    monkeypatch.setattr("webhooks_mcp.store.MAX_EVENTS_PER_NAME", object())
    with pytest.raises(sqlite3.Error):
        await store.sweep()
    await store.mint("later")  # an unrelated commit must not persist the half-done sweep
    assert len(await store.list("old.e", 0, 10)) == 1


async def test_open_keeps_sqlite_temp_files_in_memory(store):
    assert store._db.execute("PRAGMA temp_store").fetchone()[0] == 2


async def test_close_waits_for_an_in_flight_insert_of_a_cancelled_caller(tmp_path, clock):
    path = tmp_path / "close.db"
    s = EventStore(path, clock)
    await s.open()
    s._write_lock.acquire()  # a statement is "running" in the worker thread
    try:
        caller = asyncio.create_task(s.insert("a.b", '{"n":1}', None, None))
        await asyncio.sleep(0.05)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        closing = asyncio.create_task(s.close())
        await asyncio.sleep(0.1)
        assert not closing.done()
    finally:
        s._write_lock.release()
    await asyncio.wait_for(closing, 5)
    reopened = EventStore(path, clock)
    await reopened.open()
    try:
        assert [e.event_name for e in await reopened.list("a.b", 0, 10)] == ["a.b"]
    finally:
        await reopened.close()
