import logging
from datetime import timedelta

import pytest

from mcp_fixtures import KEY, serve
from oauth_fixtures import ISSUER, RESOURCE
from webhooks_mcp.config import Settings
from webhooks_mcp.constants import JWKS_MAX_AGE_SECONDS, JWKS_REFETCH_MIN_SECONDS
from webhooks_mcp.mcp_app import build_mcp_app

pytest_plugins = ["mcp_fixtures", "oauth_fixtures"]

MODES = ["static+oauth", "oauth"]
META = "https://wm.example/.well-known/oauth-protected-resource/mcp"
WELL_KNOWN = "/.well-known/oauth-protected-resource/mcp"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
NO_TOKEN = f'Bearer resource_metadata="{META}"'
BAD_TOKEN = f'Bearer error="invalid_token", resource_metadata="{META}"'
NO_SCOPE = f'Bearer error="insufficient_scope", scope="webhooks-mcp", resource_metadata="{META}"'
LOGGER = "webhooks_mcp.mcp_app"


def _bearer(token: str) -> dict[str, str]:
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}"}


async def test_static_mode_has_no_metadata_route(mcp_http):
    assert (await mcp_http.get(WELL_KNOWN)).status_code == 404


async def test_ingest_has_no_metadata_route(ingest):
    assert (await ingest.get(WELL_KNOWN)).status_code == 404


@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_metadata_body(oauth_http):
    r = await oauth_http.get(WELL_KNOWN)
    assert (r.status_code, r.json()) == (
        200,
        {
            "resource": RESOURCE,
            "authorization_servers": [ISSUER],
            "scopes_supported": ["webhooks-mcp"],
            "bearer_methods_supported": ["header"],
        },
    )


@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_no_credentials_is_401_with_resource_metadata(oauth_http):
    r = await oauth_http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Host": "evil.example"})
    assert (r.status_code, r.json(), r.headers["www-authenticate"]) == (
        401,
        {"error": "unauthorized"},
        NO_TOKEN,
    )


@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
@pytest.mark.parametrize(
    "value", ["Basic x", "Bearer ", "Bearer  t", "Bearer a b", "bearer KEY", "bearer TOKEN"]
)
async def test_malformed_header_is_401_without_error(oauth_http, issuer, value):
    value = value.replace("KEY", KEY).replace("TOKEN", issuer.token())
    r = await oauth_http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": value})
    assert (r.status_code, r.headers["www-authenticate"]) == (401, NO_TOKEN)


@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_non_ascii_header_is_401(oauth_http):
    headers = [*MCP_HEADERS.items(), (b"authorization", b"Bearer \xff\xfe")]
    r = await oauth_http.post("/mcp", json=INIT, headers=headers)
    assert (r.status_code, r.headers["www-authenticate"]) == (401, NO_TOKEN)


@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_two_authorization_headers_is_401(oauth_http, issuer):
    good = f"Bearer {issuer.token()}"
    headers = [*MCP_HEADERS.items(), ("Authorization", good), ("Authorization", good)]
    r = await oauth_http.post("/mcp", json=INIT, headers=headers)
    assert (r.status_code, r.headers["www-authenticate"]) == (401, NO_TOKEN)


@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_refused_token_is_401_invalid_token(oauth_http, issuer):
    # A non-JWT that is not the static key: a wrong static key in static+oauth, an old key in oauth.
    for t in (issuer.token(aud="https://wm.example/other"), "x" * 40):
        r = await oauth_http.post("/mcp", json=INIT, headers=_bearer(t))
        assert (r.status_code, r.headers["www-authenticate"]) == (401, BAD_TOKEN)


@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_valid_token_reaches_mcp_with_or_without_sub(oauth_http, issuer):
    for t in (issuer.token(), issuer.token(drop=("sub",))):
        r = await oauth_http.post("/mcp", json=INIT, headers=_bearer(t))
        assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"]


@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_token_without_scope_is_403(oauth_http, issuer):
    for kw in ({"scp": ["offline_access"]}, {"drop": ("scp",)}, {"scp": "webhooks-mcp"}):
        r = await oauth_http.post("/mcp", json=INIT, headers=_bearer(issuer.token(**kw)))
        assert (r.status_code, r.json(), r.headers["www-authenticate"]) == (
            403,
            {"error": "insufficient_scope"},
            NO_SCOPE,
        )


async def test_issuer_down_and_no_key_is_503(oauth_http, issuer):
    issuer.down = True
    r = await oauth_http.post("/mcp", json=INIT, headers=_bearer(issuer.token()))
    assert (r.status_code, r.json()) == (503, {"error": "issuer unavailable"})
    assert "www-authenticate" not in r.headers


@pytest.mark.parametrize("oauth_http", ["static+oauth"], indirect=True)
async def test_static_key_still_works_while_issuer_down(oauth_http, issuer):
    issuer.down = True
    r = await oauth_http.post("/mcp", json=INIT, headers=_bearer(KEY))
    assert r.status_code == 200 and issuer.requests == []


@pytest.mark.parametrize("mode", MODES)
async def test_default_verifier_never_contacts_the_issuer_at_start(store, tmp_path, mode):
    key = KEY if mode == "static+oauth" else None
    settings = Settings(key, {"default": "https://hooks.example"}, tmp_path, mode, ISSUER, RESOURCE)
    async with serve(build_mcp_app(store, settings)) as client:
        r = await client.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert (r.status_code, r.headers["www-authenticate"]) == (401, NO_TOKEN)
        assert (await client.get(WELL_KNOWN)).status_code == 200
        if key:
            assert (await client.post("/mcp", json=INIT, headers=_bearer(key))).status_code == 200


def _refusals(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


async def test_refusals_log_mode_and_reason_only(oauth_http, issuer, clock, caplog):
    caplog.set_level(logging.INFO, logger="webhooks_mcp")
    bad = issuer.token(aud="https://x.example", sub="sub-secret-7")
    unscoped = issuer.token(scp=["other"], sub="sub-secret-7")
    assert (await oauth_http.post("/mcp", json=INIT, headers=MCP_HEADERS)).status_code == 401
    assert (await oauth_http.post("/mcp", json=INIT, headers=_bearer(bad))).status_code == 401
    assert (await oauth_http.post("/mcp", json=INIT, headers=_bearer(unscoped))).status_code == 403
    clock.advance(timedelta(seconds=JWKS_REFETCH_MIN_SECONDS + 1))
    issuer.down = True
    fresh_kid = issuer.token(header={"kid": "k9"})
    r = await oauth_http.post("/mcp", json=INIT, headers=_bearer(fresh_kid))
    assert r.status_code == 401  # the cached set is within JWKS_MAX_AGE_SECONDS
    assert _refusals(caplog) == [
        "mcp auth refused: mode=oauth reason=missing",
        "mcp auth refused: mode=oauth reason=invalid",
        "mcp auth refused: mode=oauth reason=scope",
        "mcp auth refused: mode=oauth reason=invalid",
    ]
    assert not any(s in caplog.text for s in (bad, unscoped, fresh_kid, KEY, "sub-secret-7"))


async def test_expired_key_set_with_issuer_down_is_503(oauth_http, issuer, clock):
    ok = await oauth_http.post("/mcp", json=INIT, headers=_bearer(issuer.token()))
    assert ok.status_code == 200
    clock.advance(timedelta(seconds=JWKS_MAX_AGE_SECONDS + 1))
    issuer.down = True
    r = await oauth_http.post("/mcp", json=INIT, headers=_bearer(issuer.token()))
    assert (r.status_code, r.json()) == (503, {"error": "issuer unavailable"})
    assert "www-authenticate" not in r.headers


async def test_issuer_unavailable_is_logged(oauth_http, issuer, caplog):
    caplog.set_level(logging.INFO, logger="webhooks_mcp")
    issuer.down = True
    r = await oauth_http.post("/mcp", json=INIT, headers=_bearer(issuer.token()))
    assert r.status_code == 503
    assert _refusals(caplog) == ["mcp auth refused: mode=oauth reason=issuer_unavailable"]


async def test_static_mode_logs_no_refusal(mcp_http, caplog):
    caplog.set_level(logging.DEBUG)
    await mcp_http.post("/mcp", json=INIT, headers=MCP_HEADERS)
    assert not [r for r in caplog.records if r.name == LOGGER]
