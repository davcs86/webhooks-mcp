import asyncio
from datetime import timedelta

import httpx2
import pytest

from oauth_fixtures import DISCOVERY, ISSUER
from webhooks_mcp.constants import (
    JWKS_MAX_AGE_SECONDS,
    JWKS_MAX_RESPONSE_BYTES,
    JWKS_REFETCH_MIN_SECONDS,
)
from webhooks_mcp.jwks import IssuerUnavailable

pytest_plugins = ["oauth_fixtures"]


async def test_first_key_fetches_discovery_then_jwks(jwks_cache, issuer):
    assert (await jwks_cache.key("k1")).key_id == "k1"
    assert issuer.requests == [DISCOVERY, "/jwks"]


async def test_cached_key_needs_no_fetch(jwks_cache, issuer):
    await jwks_cache.key("k1")
    await jwks_cache.key("k1")
    assert len(issuer.requests) == 2


async def test_keys_refresh_after_max_age(jwks_cache, issuer, clock):
    await jwks_cache.key("k1")
    clock.advance(timedelta(seconds=JWKS_MAX_AGE_SECONDS + 1))
    await jwks_cache.key("k1")
    assert len(issuer.requests) == 4


async def test_unknown_kid_refetch_is_rate_limited_after_success(jwks_cache, issuer, clock):
    await jwks_cache.key("k1")
    clock.advance(timedelta(seconds=JWKS_REFETCH_MIN_SECONDS - 1))
    assert await jwks_cache.key("k2") is None and len(issuer.requests) == 2
    issuer.add_key("k2")
    clock.advance(timedelta(seconds=2))
    assert (await jwks_cache.key("k2")).key_id == "k2" and len(issuer.requests) == 4
    assert await jwks_cache.key("k3") is None and len(issuer.requests) == 4


async def test_retry_after_failure_is_rate_limited(jwks_cache, issuer, clock):
    issuer.down = True
    with pytest.raises(IssuerUnavailable):
        await jwks_cache.key("k1")
    issuer.down = False
    with pytest.raises(IssuerUnavailable):  # within the limit: no attempt
        await jwks_cache.key("k1")
    assert issuer.requests == [DISCOVERY]
    clock.advance(timedelta(seconds=JWKS_REFETCH_MIN_SECONDS + 1))
    assert (await jwks_cache.key("k1")).key_id == "k1"


async def test_concurrent_lookups_share_one_fetch(jwks_cache, issuer):
    issuer.gate = asyncio.Event()
    tasks = [asyncio.create_task(jwks_cache.key("k1")) for _ in range(5)]
    await asyncio.sleep(0.01)
    issuer.gate.set()
    assert all(k.key_id == "k1" for k in await asyncio.gather(*tasks))
    assert issuer.requests == [DISCOVERY, "/jwks"]


async def test_failed_fetch_keeps_a_set_within_max_age(jwks_cache, issuer, clock):
    await jwks_cache.key("k1")
    clock.advance(timedelta(seconds=JWKS_REFETCH_MIN_SECONDS + 1))
    issuer.down = True
    assert await jwks_cache.key("k2") is None  # a set within max age → 401, not 503
    assert (await jwks_cache.key("k1")).key_id == "k1"


@pytest.mark.parametrize("fail", ["down", "timeout"])
async def test_expired_set_with_refetch_failing_raises(
    jwks_cache, issuer, clock, monkeypatch, fail
):
    await jwks_cache.key("k1")
    clock.advance(timedelta(seconds=JWKS_MAX_AGE_SECONDS + 1))
    if fail == "down":
        issuer.down = True
    else:
        monkeypatch.setattr("webhooks_mcp.jwks.JWKS_FETCH_TIMEOUT_SECONDS", 0.05)
        issuer.delay = 1
    with pytest.raises(IssuerUnavailable):  # k1 is in the expired set
        await jwks_cache.key("k1")
    assert issuer.requests == [DISCOVERY, "/jwks", DISCOVERY]


async def test_expired_set_with_refetch_rate_limited_raises(jwks_cache, issuer, clock):
    await jwks_cache.key("k1")
    clock.advance(timedelta(seconds=JWKS_MAX_AGE_SECONDS - 1))
    issuer.down = True
    assert await jwks_cache.key("k2") is None  # the attempt fails; the set is still in age
    issuer.down = False
    clock.advance(timedelta(seconds=2))  # set expired, last attempt 2 s ago
    with pytest.raises(IssuerUnavailable):
        await jwks_cache.key("k1")
    assert len(issuer.requests) == 3  # no attempt within the limit


@pytest.mark.parametrize(
    "break_issuer",
    [
        lambda i: i.discovery.update(issuer=ISSUER + "/"),
        lambda i: i.discovery.update(jwks_uri="http://auth.example/jwks"),
        lambda i: i.discovery.update(jwks_uri="https://other.example/jwks"),
        lambda i: i.override.update({DISCOVERY: httpx2.Response(500)}),
        lambda i: i.override.update(
            {"/jwks": httpx2.Response(302, headers={"Location": "/jwks2"})}
        ),
        lambda i: i.override.update({DISCOVERY: httpx2.Response(200, content=b"{not json")}),
        lambda i: i.override.update({DISCOVERY: httpx2.Response(200, json=["not", "an", "obj"])}),
        lambda i: i.override.update({"/jwks": httpx2.Response(200, json={"keys": []})}),
        lambda i: i.jwks["keys"][0].pop("kid"),
    ],
)
async def test_bad_issuer_responses_are_failed_fetches(jwks_cache, issuer, break_issuer):
    break_issuer(issuer)
    with pytest.raises(IssuerUnavailable):
        await jwks_cache.key("k1")


async def test_streamed_response_over_the_cap_stops_reading(jwks_cache, issuer):
    sent = 0

    async def chunks():
        nonlocal sent
        for _ in range(128):
            sent += 1
            yield b"x" * 8192

    issuer.override["/jwks"] = httpx2.Response(200, content=chunks())  # no Content-Length
    with pytest.raises(IssuerUnavailable):
        await jwks_cache.key("k1")
    assert sent <= JWKS_MAX_RESPONSE_BYTES // 8192 + 1


async def test_fetch_timeout_is_a_failed_fetch(jwks_cache, issuer, monkeypatch):
    monkeypatch.setattr("webhooks_mcp.jwks.JWKS_FETCH_TIMEOUT_SECONDS", 0.05)
    issuer.delay = 1
    with pytest.raises(IssuerUnavailable):
        await jwks_cache.key("k1")
