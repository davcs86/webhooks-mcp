import asyncio
import os
import re
import time
import uuid
from datetime import timedelta

import pytest

from webhooks_mcp.constants import EVENT_TTL, MAX_EVENTS_PER_NAME
from webhooks_mcp.store import EventStore, is_valid_event_name


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
    with pytest.raises(Exception):  # noqa: B017 - sqlite3.OperationalError or OSError
        await EventStore(blocker / "events.db").open()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
async def test_open_fails_on_unwritable_path(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir(mode=0o500)
    with pytest.raises(Exception):  # noqa: B017 - sqlite3.OperationalError or OSError
        await EventStore(ro / "events.db").open()
