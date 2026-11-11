from datetime import timedelta

import pytest

from webhooks_mcp.constants import EVENT_TTL, MAX_BODY_BYTES


async def test_typed_post_stores_and_returns_201(ingest, store):
    r = await ingest.post(
        "/hooks",
        json={"event_name": "a.b", "payload": {"x": 1}},
        headers={"X-Forwarded-For": "100.64.0.7, 10.0.0.1"},
    )
    assert r.status_code == 201 and r.json()["duplicate"] is False
    e = await store.get(r.json()["event_id"])
    assert (e.event_name, e.payload, e.source_ip) == ("a.b", {"x": 1}, "100.64.0.7")


async def test_source_ip_falls_back_to_socket_peer(ingest, store):
    r = await ingest.post("/hooks", json={"event_name": "a.b", "payload": 1})
    e = await store.get(r.json()["event_id"])
    assert e.source_ip is not None


async def test_payload_stored_compact(ingest, store):
    r = await ingest.post(
        "/hooks", content='{"event_name": "a.b", "payload": {"x": 1, "y": "é"}}'.encode()
    )
    e = await store.get(r.json()["event_id"])
    assert e.payload_bytes == len('{"x":1,"y":"é"}'.encode())


async def test_idempotent_repeat_returns_200_same_id(ingest):
    h = {"Idempotency-Key": "k1"}
    body = {"event_name": "a.b", "payload": 1}
    a = await ingest.post("/hooks", json=body, headers=h)
    b = await ingest.post("/hooks", json=body, headers=h)
    assert (b.status_code, b.json()) == (200, {"event_id": a.json()["event_id"], "duplicate": True})


async def test_empty_idempotency_key_is_absent(ingest):
    body = {"event_name": "a.b", "payload": 1}
    a = await ingest.post("/hooks", json=body, headers={"Idempotency-Key": ""})
    b = await ingest.post("/hooks", json=body, headers={"Idempotency-Key": ""})
    assert (a.status_code, b.status_code) == (201, 201)
    assert a.json()["event_id"] != b.json()["event_id"]


async def test_idempotency_key_length_cap(ingest):
    body = {"event_name": "a.b", "payload": 1}
    ok = await ingest.post("/hooks", json=body, headers={"Idempotency-Key": "k" * 255})
    bad = await ingest.post("/hooks", json=body, headers={"Idempotency-Key": "k" * 256})
    assert (ok.status_code, bad.status_code) == (201, 422)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1,2]",
        b'"x"',
        b"null",
        b'{"payload":1}',
        b'{"event_name":"A.b","payload":1}',
        b'{"event_name":"a.b"}',
        b'{"event_name":"minted.x-aaaaaaaaaaaa","payload":1}',
        b'{"event_name":5,"payload":1}',
    ],
)
async def test_typed_rejects_bad_bodies_with_422(ingest, body):
    r = await ingest.post("/hooks", content=body)
    assert r.status_code == 422 and "error" in r.json()


async def test_error_does_not_echo_body(ingest):
    r = await ingest.post("/hooks", content=b'{"event_name":"A.b","payload":"sekret-value"}')
    assert "sekret-value" not in r.text


async def test_payload_null_is_accepted(ingest):
    r = await ingest.post("/hooks", json={"event_name": "a.b", "payload": None})
    assert r.status_code == 201


async def test_minted_post_stores_whole_body(ingest, store):
    m = await store.mint("job")
    r = await ingest.post(
        f"/hooks/{m.event_name}", json={"event_name": "ignored", "payload": {"y": 2}}
    )
    e = await store.get(r.json()["event_id"])
    assert (r.status_code, e.event_name, e.payload) == (
        201,
        m.event_name,
        {"event_name": "ignored", "payload": {"y": 2}},
    )


async def test_minted_accepts_any_json_value(ingest, store):
    m = await store.mint("job")
    r = await ingest.post(f"/hooks/{m.event_name}", content=b"[1,2]")
    assert r.status_code == 201
    assert (await store.get(r.json()["event_id"])).payload == [1, 2]


async def test_unknown_minted_name_is_404(ingest):
    assert (await ingest.post("/hooks/minted.nope-aaaaaaaaaaaa", json={})).status_code == 404


async def test_expired_minted_name_is_404(ingest, store, clock):
    m = await store.mint("job")
    clock.advance(EVENT_TTL + timedelta(seconds=1))
    r = await ingest.post(f"/hooks/{m.event_name}", json={"a": 1})
    assert r.status_code == 404 and "error" in r.json()


async def test_minted_rejects_non_json_with_422(ingest, store):
    m = await store.mint("job")
    r = await ingest.post(f"/hooks/{m.event_name}", content=b"not json")
    assert r.status_code == 422 and "error" in r.json()


async def test_minted_idempotency(ingest, store):
    m = await store.mint("job")
    h = {"Idempotency-Key": "k"}
    a = await ingest.post(f"/hooks/{m.event_name}", json={"a": 1}, headers=h)
    b = await ingest.post(f"/hooks/{m.event_name}", json={"a": 1}, headers=h)
    assert (a.status_code, b.status_code) == (201, 200)


async def test_body_over_cap_is_413(ingest):
    r = await ingest.post("/hooks", content=b"x" * (MAX_BODY_BYTES + 1))
    assert r.status_code == 413


async def test_minted_body_over_cap_is_413(ingest, store):
    m = await store.mint("job")
    r = await ingest.post(f"/hooks/{m.event_name}", content=b"x" * (MAX_BODY_BYTES + 1))
    assert r.status_code == 413


async def test_chunked_body_over_cap_is_413(ingest):
    async def chunks():
        for _ in range(40):
            yield b"x" * 8192  # 320 KiB, no Content-Length

    assert (await ingest.post("/hooks", content=chunks())).status_code == 413


async def test_healthz(ingest):
    assert (await ingest.get("/healthz")).status_code == 200


async def test_mcp_route_absent(ingest):
    assert (await ingest.post("/mcp", json={})).status_code == 404


async def test_get_hooks_is_405(ingest):
    assert (await ingest.get("/hooks")).status_code == 405
