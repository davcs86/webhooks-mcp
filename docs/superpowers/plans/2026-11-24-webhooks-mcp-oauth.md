# webhooks-mcp OAuth Resource Server Implementation Plan

> **Status: not started.** Waits for the owner's review of this plan and answers to the open questions at the end.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `AUTH_MODE` `static` / `static+oauth` / `oauth` on the MCP listener. The OAuth modes accept RS256 JWT access
tokens from one issuer and serve RFC 9728 protected resource metadata. `static`, the default, keeps today's
behaviour byte for byte.

**Architecture:** `config.py` parses and refuses the mode and the `OAUTH_*` variables. A new `jwks.py` (`JwksCache`)
fetches the issuer's discovery document and JWKS through an injected `httpx2.AsyncClient`, with the cache, rate limit,
timeout and size cap all on the injected clock. A new `token_verifier.py` (`JwksTokenVerifier`, the SDK's
`TokenVerifier` protocol) turns a token into an `AccessToken` or `None`. `mcp_app.py#McpEndpoint` stays the one place a
request is accepted or refused, and `build_mcp_app` adds the metadata route in the OAuth modes. Tests use a stub issuer
served by `httpx2.MockTransport`: no network, no wall time.

**Tech Stack:** Python 3.13, MCP Python SDK 2.3.0 (`mcp.server.auth.provider.AccessToken`/`TokenVerifier`,
`mcp.server.auth.settings.AuthSettings`, `mcp.server.auth.routes.create_protected_resource_routes`/
`build_resource_metadata_url`), PyJWT 2.15.1 (`jwt.get_unverified_header`, `jwt.decode`, `jwt.PyJWKSet`), httpx2 2.16.0,
Starlette, pytest + pytest-asyncio, ruff.

**Spec:** `docs/superpowers/specs/2026-11-11-webhooks-mcp-design.md` § 6.7 (and § 6.5 for the env), `docs/contracts.md`
C1 and C4 as amended 2026-11-24 (C1 again 2026-11-27). Shared design: homelab-edge
`docs/superpowers/specs/2026-11-23-authelia-sso-respec.md` (decisions 16 and 21 as amended, § 3 "Signing keys", § 4.6,
§ 4.11, § 5 webhooks-mcp row, § 10 P4) and `docs/runbooks/sso.md` § 6b. The edge's wiring is a separate plan in
homelab-edge.

## Global Constraints

- `AUTH_MODE`: `static` (default) | `static+oauth` | `oauth`; any other value is refused at start (exit 2, as today).
- `MCP_AUTH_KEY`: required (≥ `MIN_AUTH_KEY_CHARS`) in `static` and `static+oauth`; refused at start in `oauth`.
- `OAUTH_ISSUER`, `OAUTH_RESOURCE`: required in the OAuth modes, refused in `static`. Issuer: `https:`, no trailing
  slash, query or fragment, a path allowed. Resource: `https:`, path exactly `/mcp`, no query or fragment.
- Constants, in `src/webhooks_mcp/constants.py` only, read at call time (WM-04): `REQUIRED_SCOPE = "webhooks-mcp"`,
  `TOKEN_CLOCK_SKEW_SECONDS = 60`, `JWKS_MAX_AGE_SECONDS = 3600`, `JWKS_REFETCH_MIN_SECONDS = 60`,
  `JWKS_FETCH_TIMEOUT_SECONDS = 5`, `JWKS_MAX_RESPONSE_BYTES = 65536`.
- A token is accepted only with: header `alg` `RS256`; header `typ` `at+jwt` or `application/at+jwt`
  (case-insensitive); a `kid` found in the JWKS and a valid signature; `exp`, `iss`, `aud` present; `iss` ==
  `OAUTH_ISSUER`; `aud` == or containing `OAUTH_RESOURCE`; `exp`/`nbf`/`iat` within `TOKEN_CLOCK_SKEW_SECONDS` by the
  injected clock. PyJWT's `verify_exp`/`verify_nbf`/`verify_iat` are off.
- Keys only from a set fetched within `JWKS_MAX_AGE_SECONDS`. Once the cached set is older, a token that needs a key
  triggers a refetch under the shared limit; if it fails, times out or is not allowed, → `IssuerUnavailable` (503) for
  every token, including one whose `kid` is in the old set. No expired key is ever used.
- Scopes come from `scp` when present (anything but a list of strings → no scopes, never rescued by `scope`), else from
  the space-delimited `scope` string, else none. Authorization is `REQUIRED_SCOPE in scopes`, nothing else; no subject
  list.
- WM-08: `Bearer ` is an exact, case-sensitive byte prefix in every mode; the static compare stays `hmac.compare_digest`
  against `b"Bearer " + MCP_AUTH_KEY`.
- WM-05 responses, never echoing input: 401 `{"error": "unauthorized"}`, 403 `{"error": "insufficient_scope"}`, 503
  `{"error": "issuer unavailable"}`.
- `WWW-Authenticate`: `static` → `Bearer`; OAuth 401 → `Bearer resource_metadata="<metadata URL>"`, plus
  `error="invalid_token"` for a presented, refused token; 403 →
  `Bearer error="insufficient_scope", scope="webhooks-mcp", resource_metadata="<metadata URL>"`; 503 → none.
  `<metadata URL>` = `build_resource_metadata_url(OAUTH_RESOURCE)`, computed once at start, never from `Host`.
- Do not use the SDK's `BearerAuthBackend`/`RequireAuthMiddleware` or PyJWT's `PyJWKClient`. `AuthSettings(...,
  validate_token_resource=False)` is built only to validate config and is never passed to `MCPServer`.
- Startup never contacts the issuer. No log line carries the key, a token, a claim value or a `sub` (Rule 5); `static`
  logs nothing new.
- No new package: `httpx2` and `pyjwt[crypto]` move into `[project].dependencies` (already locked through `mcp`: 2.16.0
  and 2.15.1); `uv lock` in the same change. `httpx` stays dev-only.
- Tests: `uv run pytest -W error`, `FakeClock` (`tests/conftest.py`), no network; new MCP test files declare
  `pytest_plugins` (WM-07).

## Review Focus

Inputs the spec implies but its test list does not name. Each has a test in the named task.

1. An `Authorization` value with non-ASCII bytes (`Bearer \xff\xfe`) in an OAuth mode → 401 like any malformed
   header, never a 500 from decoding it. *(T4)*
2. Claims of the wrong type: `exp`/`nbf`/`iat` as a string, a boolean, or `Infinity` (PyJWT's `json.loads` accepts it
   and its own time checks are off) → refused. `sub`/`client_id` that is not a string → the field is left empty, never a
   500 whose pydantic error text would carry the claim value into a log. *(T3)*
3. `OAUTH_ISSUER`/`OAUTH_RESOURCE` ending in a bare `?` or `#` → refused at start. `urlsplit` reports an empty
   query/fragment for both, and the exact `iss` compare would then fail every token. *(T1)*
4. An issuer response with no `Content-Length` streamed past `JWKS_MAX_RESPONSE_BYTES`, or a 3xx redirect → a failed
   fetch. The read stops at the cap and no redirect is followed. *(T2)*
5. A token with no `kid`, or a non-JWT string such as the old static key sent in `oauth` → 401 with no issuer fetch.
   It never spends the refetch budget, and never becomes a 503 while no key is cached. *(T3)*

## File Structure

| File | Responsibility |
|---|---|
| `src/webhooks_mcp/constants.py` | The six constants above |
| `src/webhooks_mcp/config.py` | `Settings` gains `auth_mode`, `oauth_issuer`, `oauth_resource`; start-time refusals |
| `src/webhooks_mcp/jwks.py` (new) | `IssuerUnavailable`, `JwksCache`: discovery + JWKS fetch, cache, rate limit, caps |
| `src/webhooks_mcp/token_verifier.py` (new) | `JwksTokenVerifier`: header, signature and claim checks → `AccessToken` |
| `src/webhooks_mcp/mcp_app.py` | `McpEndpoint` per mode, responses, refusal log, metadata route, verifier lifecycle |
| `pyproject.toml`, `uv.lock` | `httpx2`, `pyjwt[crypto]` as direct dependencies |
| `tests/mcp_fixtures.py` | `serve(app)` extracted from `mcp_http` so the OAuth fixtures reuse it |
| `tests/oauth_fixtures.py` (new) | `StubIssuer`, `jwks_cache`, `verifier`, `oauth_http` fixtures |
| `tests/test_config.py`, `tests/test_main.py` | Refusals; CLI exit 2 |
| `tests/test_jwks.py`, `tests/test_token_verifier.py`, `tests/test_mcp_auth.py` (new) | One per new behaviour |
| `README.md`, `docs/contracts.md`, spec § 6.7 heading | Auth modes documented; "not built" removed |

---

### Task 1: Configuration — `AUTH_MODE` and the `OAUTH_*` variables

**Files:**
- Modify: `src/webhooks_mcp/constants.py`, `src/webhooks_mcp/config.py`
- Test: `tests/test_config.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: `MIN_AUTH_KEY_CHARS`; `mcp.server.auth.settings.AuthSettings`.
- Produces:

```python
# constants.py
REQUIRED_SCOPE = "webhooks-mcp"
# config.py
AuthMode = Literal["static", "static+oauth", "oauth"]
AUTH_MODES: tuple[AuthMode, ...] = ("static", "static+oauth", "oauth")
OAUTH_MODES: tuple[AuthMode, ...] = ("static+oauth", "oauth")

@dataclass(frozen=True)
class Settings:
    mcp_auth_key: str | None = field(repr=False)   # None only in "oauth"
    ingest_base_urls: dict[str, str]
    data_dir: Path
    auth_mode: AuthMode = "static"
    oauth_issuer: str | None = None                 # verbatim; set only in OAUTH_MODES
    oauth_resource: str | None = None
```

The new fields come last with defaults, so `tests/mcp_fixtures.py#settings` (`Settings(KEY, {...}, tmp_path)`) keeps
building a `static` config unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/test_config.py`:

```python
ISS, RES = "https://auth.example", "https://wm.example/mcp"

def _env(mode="oauth", **over):            # value None removes the variable
    env = {"AUTH_MODE": mode, "INGEST_BASE_URLS": BASES, "OAUTH_ISSUER": ISS, "OAUTH_RESOURCE": RES}
    if mode == "static+oauth":
        env["MCP_AUTH_KEY"] = K
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}

def test_auth_mode_defaults_to_static():
    s = Settings.from_env({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": BASES})
    assert (s.auth_mode, s.oauth_issuer, s.oauth_resource) == ("static", None, None)

@pytest.mark.parametrize("mode,key", [("static+oauth", K), ("oauth", None)])
def test_oauth_modes_parse(mode, key):
    s = Settings.from_env(_env(mode))
    assert (s.auth_mode, s.mcp_auth_key, s.oauth_issuer, s.oauth_resource) == (mode, key, ISS, RES)

def test_issuer_keeps_its_path_verbatim():
    assert Settings.from_env(_env(OAUTH_ISSUER=ISS + "/realm")).oauth_issuer == ISS + "/realm"

@pytest.mark.parametrize("env,var", [
    (_env(AUTH_MODE="OAuth"), "AUTH_MODE"),
    (_env(AUTH_MODE=""), "AUTH_MODE"),                                       # Open question 1
    ({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": BASES, "OAUTH_ISSUER": ISS}, "OAUTH_ISSUER"),
    ({"MCP_AUTH_KEY": K, "INGEST_BASE_URLS": BASES, "OAUTH_RESOURCE": RES}, "OAUTH_RESOURCE"),
    (_env("static+oauth", MCP_AUTH_KEY=None), "MCP_AUTH_KEY"),
    (_env("static+oauth", MCP_AUTH_KEY="short"), "MCP_AUTH_KEY"),
    (_env(MCP_AUTH_KEY=K), "MCP_AUTH_KEY"),
    (_env(MCP_AUTH_KEY=""), "MCP_AUTH_KEY"),                                 # Open question 1
    (_env(OAUTH_ISSUER=None), "OAUTH_ISSUER"),
    (_env(OAUTH_RESOURCE=None), "OAUTH_RESOURCE"),
    *[(_env(OAUTH_ISSUER=v), "OAUTH_ISSUER") for v in (
        "http://auth.example", "https://auth.example/", "https://auth.example?x=1",
        "https://auth.example#f", "https://auth.example?", "https://auth.example#",   # Review Focus 3
        "auth.example", "https://", "https://exa mple")],
    *[(_env(OAUTH_RESOURCE=v), "OAUTH_RESOURCE") for v in (
        "http://wm.example/mcp", "https://wm.example", "https://wm.example/", "https://wm.example/mcp/",
        "https://wm.example/MCP", "https://wm.example/x/mcp", "https://wm.example/mcp?x",
        "https://wm.example/mcp?", "https://wm.example/mcp#", "https://wm ex/mcp")],
])
def test_auth_refusals_name_the_variable(env, var):
    with pytest.raises(ConfigError, match=var):
        Settings.from_env(env)

def test_oauth_refusal_never_echoes_the_key():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env(_env(MCP_AUTH_KEY="s" * 40))
    assert "s" * 40 not in str(exc.value)
```

In `tests/test_main.py`, add `({**GOOD, "AUTH_MODE": "bogus"}, "AUTH_MODE")` to the parametrize list of
`test_cli_exits_2_naming_variable_on_bad_env`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_config.py tests/test_main.py -k "auth or oauth or issuer or bad_env" -v`
Expected: FAIL (`Settings` has no `auth_mode`; no refusal raised).

- [ ] **Step 3: Implement** `REQUIRED_SCOPE` in `constants.py` and `Settings.from_env` in `config.py`.

Checks run in this order, and each raises `ConfigError` with a fixed message naming the variable: `AUTH_MODE` (absent →
`static`; present and not in `AUTH_MODES` → refused); then `MCP_AUTH_KEY` per mode; then `OAUTH_*` presence per mode;
then the URL shapes; then `INGEST_BASE_URLS` as today. "Set" means present in the env, even when empty (Open
question 1). URL shape uses `urlsplit` plus a literal `"?" not in raw and "#" not in raw`. After the shape checks,
build `AuthSettings(issuer_url=issuer, resource_server_url=resource, required_scopes=[REQUIRED_SCOPE],
validate_token_resource=False)`. Map a `pydantic.ValidationError` to the variable named by
`exc.errors()[0]["loc"][0]` (`issuer_url` → `OAUTH_ISSUER`, `resource_server_url` → `OAUTH_RESOURCE`), and discard
the object.

- [ ] **Step 4: Run them to verify they pass**

Run: `uv run pytest tests/test_config.py tests/test_main.py -W error -v`
Expected: PASS, with the existing config and CLI tests unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/webhooks_mcp/constants.py src/webhooks_mcp/config.py tests/test_config.py tests/test_main.py
git commit -m "feat(config): AUTH_MODE and OAUTH_ISSUER/OAUTH_RESOURCE with start-time refusals"
```

---

### Task 2: `JwksCache` — discovery and keys under the injected clock

**Files:**
- Modify: `src/webhooks_mcp/constants.py`, `pyproject.toml`, `uv.lock`, `tests/mcp_fixtures.py`
- Create: `src/webhooks_mcp/jwks.py`, `tests/oauth_fixtures.py`, `tests/test_jwks.py`

**Interfaces:**
- Consumes: `webhooks_mcp.store.utcnow`; `tests/conftest.py#FakeClock`.
- Produces:

```python
# constants.py
JWKS_MAX_AGE_SECONDS = 3600
JWKS_REFETCH_MIN_SECONDS = 60
JWKS_FETCH_TIMEOUT_SECONDS = 5
JWKS_MAX_RESPONSE_BYTES = 65536

# jwks.py
class IssuerUnavailable(Exception): ...

class JwksCache:
    def __init__(self, issuer: str, client: httpx2.AsyncClient,
                 clock: Callable[[], datetime] = utcnow) -> None
    async def key(self, kid: str) -> jwt.PyJWK | None
        # the key; None = kid not in a set fetched within JWKS_MAX_AGE_SECONDS (→ 401);
        # raises IssuerUnavailable = no set within that age after any allowed fetch (→ 503)
    async def aclose(self) -> None   # closes the client

# tests/mcp_fixtures.py
@asynccontextmanager
async def serve(app: Starlette) -> AsyncIterator[httpx.AsyncClient]   # body moved out of mcp_http
```

`tests/oauth_fixtures.py` (a `pytest_plugins` module, WM-07) provides:

```python
ISSUER = "https://auth.example"
RESOURCE = "https://wm.example/mcp"
DISCOVERY = "/.well-known/openid-configuration"

class StubIssuer:            # one RSA key "k1", generated once per session
    requests: list[str]      # paths served, in order
    down: bool               # every request raises httpx2.ConnectError
    discovery: dict          # {"issuer": ISSUER, "jwks_uri": ISSUER + "/jwks"}
    jwks: dict               # {"keys": [public JWK, kid "k1"]}
    override: dict[str, httpx2.Response]   # path → canned response
    gate: asyncio.Event | None             # when set, the handler awaits it before answering
    delay: float                           # handler sleeps this long (asyncio.sleep) before answering
    async def handler(self, request: httpx2.Request) -> httpx2.Response
    def add_key(self, kid: str) -> None
    def token(self, *, drop: tuple[str, ...] = (), header: dict | None = None,
              key: RSAPrivateKey | None = None, **claims) -> str
        # defaults: iss ISSUER, aud RESOURCE, sub "u1", exp now+3600 (FakeClock), scp ["webhooks-mcp"];
        # header {"kid": "k1", "typ": "at+jwt"} updated by `header`; a value None removes that key
        # (`typ: None` is passed through: PyJWT then omits `typ`; it adds "JWT" when the key is absent)
def unsigned(header: dict, claims: dict) -> str   # base64url JWT with an empty signature

# fixtures: issuer(clock) -> StubIssuer; jwks_cache(issuer, clock) -> JwksCache over
# httpx2.AsyncClient(transport=httpx2.MockTransport(issuer.handler))
```

- [ ] **Step 1: Add the dependencies.** In `pyproject.toml` `[project].dependencies`, add `"httpx2"` and
  `"pyjwt[crypto]"`, unbounded like `starlette` and `uvicorn`. Run `uv lock`.
  Expected: `git diff uv.lock` touches only the `webhooks-mcp` package's dependency lists. `httpx2` stays 2.16.0 and
  `pyjwt` 2.15.1, and `uv lock --check` passes.

- [ ] **Step 2: Write the failing tests** in `tests/test_jwks.py` (`pytest_plugins = ["oauth_fixtures"]`):

```python
async def test_first_key_fetches_discovery_then_jwks(jwks_cache, issuer):
    assert (await jwks_cache.key("k1")).key_id == "k1"
    assert issuer.requests == [DISCOVERY, "/jwks"]

async def test_cached_key_needs_no_fetch(jwks_cache, issuer):
    await jwks_cache.key("k1"); await jwks_cache.key("k1")
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
    with pytest.raises(IssuerUnavailable): await jwks_cache.key("k1")
    issuer.down = False
    with pytest.raises(IssuerUnavailable): await jwks_cache.key("k1")    # within the limit: no attempt
    assert issuer.requests == [DISCOVERY]
    clock.advance(timedelta(seconds=JWKS_REFETCH_MIN_SECONDS + 1))
    assert (await jwks_cache.key("k1")).key_id == "k1"

async def test_concurrent_lookups_share_one_fetch(jwks_cache, issuer):
    issuer.gate = asyncio.Event()
    tasks = [asyncio.create_task(jwks_cache.key("k1")) for _ in range(5)]
    await asyncio.sleep(0.01); issuer.gate.set()
    assert all(k.key_id == "k1" for k in await asyncio.gather(*tasks))
    assert issuer.requests == [DISCOVERY, "/jwks"]

async def test_failed_fetch_keeps_a_set_within_max_age(jwks_cache, issuer, clock):
    await jwks_cache.key("k1")
    clock.advance(timedelta(seconds=JWKS_REFETCH_MIN_SECONDS + 1)); issuer.down = True
    assert await jwks_cache.key("k2") is None                      # a set within max age → 401, not 503
    assert (await jwks_cache.key("k1")).key_id == "k1"

@pytest.mark.parametrize("fail", ["down", "timeout"])              # Open question 3: answered no
async def test_expired_set_with_refetch_failing_raises(jwks_cache, issuer, clock, monkeypatch, fail):
    await jwks_cache.key("k1")
    clock.advance(timedelta(seconds=JWKS_MAX_AGE_SECONDS + 1))
    if fail == "down":
        issuer.down = True
    else:
        monkeypatch.setattr("webhooks_mcp.jwks.JWKS_FETCH_TIMEOUT_SECONDS", 0.05); issuer.delay = 1
    with pytest.raises(IssuerUnavailable): await jwks_cache.key("k1")     # k1 is in the expired set
    assert issuer.requests == [DISCOVERY, "/jwks", DISCOVERY]

async def test_expired_set_with_refetch_rate_limited_raises(jwks_cache, issuer, clock):
    await jwks_cache.key("k1")
    clock.advance(timedelta(seconds=JWKS_MAX_AGE_SECONDS - 1)); issuer.down = True
    assert await jwks_cache.key("k2") is None                      # the attempt fails; the set is still in age
    issuer.down = False
    clock.advance(timedelta(seconds=2))                            # set expired, last attempt 2 s ago
    with pytest.raises(IssuerUnavailable): await jwks_cache.key("k1")
    assert len(issuer.requests) == 3                               # no attempt within the limit

@pytest.mark.parametrize("break_issuer", [            # each is a failed fetch
    lambda i: i.discovery.update(issuer=ISSUER + "/"),
    lambda i: i.discovery.update(jwks_uri="http://auth.example/jwks"),
    lambda i: i.discovery.update(jwks_uri="https://other.example/jwks"),
    lambda i: i.override.update({DISCOVERY: httpx2.Response(500)}),
    lambda i: i.override.update({"/jwks": httpx2.Response(302, headers={"Location": "/jwks2"})}),  # Review Focus 4
    lambda i: i.override.update({DISCOVERY: httpx2.Response(200, content=b"{not json")}),
    lambda i: i.override.update({DISCOVERY: httpx2.Response(200, json=["not", "an", "object"])}),
    lambda i: i.override.update({"/jwks": httpx2.Response(200, json={"keys": []})}),             # Open question 4
    lambda i: i.jwks["keys"][0].pop("kid"),                       # a valid key without a kid: Open question 4
])
async def test_bad_issuer_responses_are_failed_fetches(jwks_cache, issuer, break_issuer):
    break_issuer(issuer)
    with pytest.raises(IssuerUnavailable):
        await jwks_cache.key("k1")

async def test_streamed_response_over_the_cap_stops_reading(jwks_cache, issuer):  # Review Focus 4
    sent = 0
    async def chunks():
        nonlocal sent
        for _ in range(128):
            sent += 1; yield b"x" * 8192
    issuer.override["/jwks"] = httpx2.Response(200, content=chunks())       # no Content-Length
    with pytest.raises(IssuerUnavailable): await jwks_cache.key("k1")
    assert sent <= JWKS_MAX_RESPONSE_BYTES // 8192 + 1

async def test_fetch_timeout_is_a_failed_fetch(jwks_cache, issuer, monkeypatch):
    monkeypatch.setattr("webhooks_mcp.jwks.JWKS_FETCH_TIMEOUT_SECONDS", 0.05)
    issuer.delay = 1
    with pytest.raises(IssuerUnavailable): await jwks_cache.key("k1")
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_jwks.py -v`
Expected: FAIL at import (`webhooks_mcp.jwks` does not exist).

- [ ] **Step 4: Implement `JwksCache` in `src/webhooks_mcp/jwks.py`** and the four constants.

State: `keys: dict[str, PyJWK]` (empty until the first success), `fetched_at`, `last_attempt` (both epoch seconds from
`clock().timestamp()`), and one `asyncio.Lock`. `key(kid)` returns at once, with no lock, when `kid` is cached and
`now - fetched_at < JWKS_MAX_AGE_SECONDS`. Otherwise it takes the lock and re-checks. It then attempts a fetch only
when `last_attempt` is unset or `now - last_attempt >= JWKS_REFETCH_MIN_SECONDS`, and sets `last_attempt = now`
before the first await. A success replaces `keys` and sets `fetched_at = now`; a failure leaves both as they were.
The result: `raise IssuerUnavailable` unless `keys` is non-empty and `now - fetched_at < JWKS_MAX_AGE_SECONDS`;
otherwise `keys.get(kid)` (`None` when absent). No path returns a key from an expired set.

One attempt is `GET <issuer>/.well-known/openid-configuration`, then `GET <jwks_uri>`. Each request runs inside
`asyncio.timeout(JWKS_FETCH_TIMEOUT_SECONDS)` through `client.stream("GET", url)` (redirects not followed). It reads
`aiter_bytes()` and aborts once the total passes `JWKS_MAX_RESPONSE_BYTES`, and only status 200 counts. The discovery
document must be a JSON object with `issuer == self._issuer` (exact), and `jwks_uri` a string whose
`urlsplit` scheme is `https` and netloc equals the issuer's. The JWKS goes through `jwt.PyJWKSet.from_dict`, after an
`isinstance(doc, dict)` check, and keeps the keys whose `key_id` is a non-empty string. An empty result is a failed
fetch. Every error on that path (`httpx2.HTTPError`, `TimeoutError`, `ValueError`, `jwt.PyJWTError`, the size and
shape checks) is a failed fetch. Read the constants at call time (WM-04).

Move `mcp_http`'s lifespan body in `tests/mcp_fixtures.py` into `serve(app)` and have `mcp_http` use it. Write
`tests/oauth_fixtures.py` to the interface above.

- [ ] **Step 5: Run them to verify they pass**

Run: `uv run pytest tests/test_jwks.py tests/test_mcp_app.py -W error -v`
Expected: PASS (`test_mcp_app.py` proves the `serve` extraction changed nothing).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/webhooks_mcp/constants.py src/webhooks_mcp/jwks.py \
  tests/mcp_fixtures.py tests/oauth_fixtures.py tests/test_jwks.py
git commit -m "feat(oauth): JWKS cache with injected clock, rate limit, timeout and size cap"
```

---

### Task 3: `JwksTokenVerifier`

**Files:**
- Modify: `src/webhooks_mcp/constants.py`, `tests/oauth_fixtures.py` (add the `verifier` fixture)
- Create: `src/webhooks_mcp/token_verifier.py`, `tests/test_token_verifier.py`

**Interfaces:**
- Consumes: `JwksCache.key`, `IssuerUnavailable` (Task 2); `REQUIRED_SCOPE` is not read here.
- Produces:

```python
# constants.py
TOKEN_CLOCK_SKEW_SECONDS = 60

# token_verifier.py — implements mcp.server.auth.provider.TokenVerifier
class JwksTokenVerifier:
    def __init__(self, issuer: str, resource: str, keys: JwksCache,
                 clock: Callable[[], datetime] = utcnow) -> None
    async def verify_token(self, token: str) -> AccessToken | None   # may raise IssuerUnavailable
    async def aclose(self) -> None                                   # keys.aclose()

# oauth_fixtures.py
# fixture verifier(jwks_cache, clock) -> JwksTokenVerifier(ISSUER, RESOURCE, jwks_cache, clock)
```

`AccessToken` fields: `token` the input; `scopes` per Global Constraints; `expires_at` `int(exp)`; `resource` the
configured resource; `subject` and `client_id` the `sub`/`client_id` claims when they are strings, else `""`.

- [ ] **Step 1: Write the failing tests** in `tests/test_token_verifier.py` (`pytest_plugins = ["oauth_fixtures"]`;
  in each test that takes `clock`, `now = int(clock().timestamp())`; `SKEW = TOKEN_CLOCK_SKEW_SECONDS`):

```python
async def test_valid_token_maps_to_access_token(verifier, issuer, clock):
    t = issuer.token(scp=["webhooks-mcp", "offline_access"], client_id="webhooks-mcp-claude", exp=now + 600)
    a = await verifier.verify_token(t)
    assert (a.token, a.scopes, a.client_id, a.subject, a.resource, a.expires_at) == (
        t, ["webhooks-mcp", "offline_access"], "webhooks-mcp-claude", "u1", RESOURCE, now + 600)

@pytest.mark.parametrize("make", [                     # make(now) -> keyword arguments for issuer.token
    lambda n: {"iss": "https://other.example"}, lambda n: {"iss": ISSUER + "/"},
    lambda n: {"aud": "https://wm.example/other"}, lambda n: {"aud": ["https://wm.example/other"]},
    lambda n: {"drop": ("aud",)}, lambda n: {"drop": ("exp",)}, lambda n: {"drop": ("iss",)},
    lambda n: {"exp": n - SKEW - 1}, lambda n: {"nbf": n + SKEW + 1}, lambda n: {"iat": n + SKEW + 1},
    lambda n: {"exp": "soon"}, lambda n: {"exp": True}, lambda n: {"exp": float("inf")},   # Review Focus 2
    lambda n: {"nbf": "x"}, lambda n: {"iat": False},
    lambda n: {"header": {"typ": "JWT"}}, lambda n: {"header": {"typ": None}}, lambda n: {"header": {"typ": 1}},
])
async def test_refused_tokens_are_none(verifier, issuer, clock, make):
    assert await verifier.verify_token(issuer.token(**make(now))) is None

@pytest.mark.parametrize("typ", ["at+jwt", "AT+JWT", "application/at+jwt"])
async def test_both_typ_spellings_accepted(verifier, issuer, typ):
    assert await verifier.verify_token(issuer.token(header={"typ": typ}))

async def test_clock_skew_is_tolerated(verifier, issuer, clock):
    assert await verifier.verify_token(issuer.token(exp=now - SKEW + 1, nbf=now + SKEW - 1, iat=now + SKEW - 1))

async def test_aud_list_containing_the_resource_is_accepted(verifier, issuer):
    assert await verifier.verify_token(issuer.token(aud=["https://x.example", RESOURCE]))

async def test_bad_signature_is_refused(verifier, issuer):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert await verifier.verify_token(issuer.token(key=other)) is None

async def test_unknown_kid_is_refused_when_a_key_is_cached(verifier, issuer):
    assert await verifier.verify_token(issuer.token())
    assert await verifier.verify_token(issuer.token(header={"kid": "nope"})) is None

async def test_unknown_kid_with_no_key_cached_raises(verifier, issuer):
    issuer.down = True
    with pytest.raises(IssuerUnavailable):
        await verifier.verify_token(issuer.token())

@pytest.mark.parametrize("make", [                                       # Review Focus 5
    lambda i: unsigned({"alg": "none", "typ": "at+jwt", "kid": "k1"}, {"iss": ISSUER}),
    lambda i: jwt.encode({"iss": ISSUER}, "s" * 64, algorithm="HS256", headers={"kid": "k1", "typ": "at+jwt"}),
    lambda i: i.token(header={"kid": None}),
    lambda i: "k" * 40, lambda i: "not.a.jwt", lambda i: "",
])
async def test_refused_before_any_fetch(verifier, issuer, make):
    issuer.down = True                       # no key cached: a fetch would raise, so None proves none happened
    assert await verifier.verify_token(make(issuer)) is None and issuer.requests == []

@pytest.mark.parametrize("kw,scopes", [
    ({"scp": ["webhooks-mcp"]}, ["webhooks-mcp"]),
    ({"drop": ("scp",), "scope": "openid webhooks-mcp"}, ["openid", "webhooks-mcp"]),
    ({"drop": ("scp",)}, []),
    ({"scp": "webhooks-mcp"}, []), ({"scp": [1]}, []),
    ({"scp": "webhooks-mcp", "scope": "webhooks-mcp"}, []),     # a malformed scp is never rescued
])
async def test_scopes(verifier, issuer, kw, scopes):
    assert (await verifier.verify_token(issuer.token(**kw))).scopes == scopes

async def test_missing_or_non_string_sub_and_client_id_are_empty(verifier, issuer):   # Review Focus 2
    for kw in ({"drop": ("sub",)}, {"sub": 7, "client_id": ["x"]}):
        a = await verifier.verify_token(issuer.token(**kw))
        assert (a.subject, a.client_id) == ("", "")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_token_verifier.py -v`
Expected: FAIL at import (`webhooks_mcp.token_verifier` does not exist).

- [ ] **Step 3: Implement `JwksTokenVerifier.verify_token`** in `src/webhooks_mcp/token_verifier.py`.

The order: `jwt.get_unverified_header` (a `jwt.PyJWTError` → `None`); header `alg == "RS256"`; `typ` a string whose
`.lower()` is `at+jwt` or `application/at+jwt`; `kid` a non-empty string. Only then `await self._keys.key(kid)`
(`None` → `None`; let `IssuerUnavailable` propagate). Then `jwt.decode(token, key=pyjwk, algorithms=["RS256"],
issuer=..., audience=..., options={"require": ["exp", "iss", "aud"], "verify_exp": False, "verify_nbf": False,
"verify_iat": False})` (a `jwt.PyJWTError` → `None`). Then the time checks on the injected clock: each present
`exp`/`nbf`/`iat` must be an `int` or `float`, not `bool`, and `math.isfinite`, and `exp > now - SKEW`,
`nbf <= now + SKEW`, `iat <= now + SKEW`. Read `TOKEN_CLOCK_SKEW_SECONDS` at call time.

- [ ] **Step 4: Run them to verify they pass**

Run: `uv run pytest tests/test_token_verifier.py tests/test_jwks.py -W error -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/webhooks_mcp/constants.py src/webhooks_mcp/token_verifier.py tests/oauth_fixtures.py \
  tests/test_token_verifier.py
git commit -m "feat(oauth): JwksTokenVerifier for RS256 at+jwt access tokens"
```

---

### Task 4: `McpEndpoint` modes, responses, metadata route, logs and docs

**Files:**
- Modify: `src/webhooks_mcp/mcp_app.py`, `tests/oauth_fixtures.py` (add `oauth_http`), `README.md`,
  `docs/contracts.md`, `docs/superpowers/specs/2026-11-11-webhooks-mcp-design.md` (§ 6.7 heading only)
- Create: `tests/test_mcp_auth.py`

**Interfaces:**
- Consumes: `Settings`, `OAUTH_MODES` (Task 1); `JwksCache`, `IssuerUnavailable` (Task 2); `JwksTokenVerifier`
  (Task 3); `REQUIRED_SCOPE`; `serve` (Task 2).
- Produces:

```python
# mcp_app.py
def build_mcp_app(store: EventStore, settings: Settings,
                  verifier: JwksTokenVerifier | None = None) -> Starlette
    # In OAUTH_MODES with verifier None, it builds
    # JwksTokenVerifier(issuer, resource, JwksCache(issuer, httpx2.AsyncClient())).
    # The lifespan closes the verifier, injected or not. In static the verifier is unused.

# oauth_fixtures.py
# fixture oauth_http(request, store, issuer, verifier, tmp_path) -> httpx.AsyncClient
#   mode = getattr(request, "param", "oauth"); Settings(KEY if mode == "static+oauth" else None,
#   {"default": "https://hooks.example"}, tmp_path, mode, ISSUER, RESOURCE); served via serve(build_mcp_app(...))
```

- [ ] **Step 1: Write the failing tests** in `tests/test_mcp_auth.py`
  (`pytest_plugins = ["mcp_fixtures", "oauth_fixtures"]`; `MODES = ["static+oauth", "oauth"]`;
  `META = "https://wm.example/.well-known/oauth-protected-resource/mcp"`; `WELL_KNOWN` its path;
  `INIT`/`MCP_HEADERS` as in `test_mcp_app.py`):

```python
NO_TOKEN = f'Bearer resource_metadata="{META}"'
BAD_TOKEN = f'Bearer error="invalid_token", resource_metadata="{META}"'
NO_SCOPE = f'Bearer error="insufficient_scope", scope="webhooks-mcp", resource_metadata="{META}"'

async def test_static_mode_has_no_metadata_route(mcp_http):
    assert (await mcp_http.get(WELL_KNOWN)).status_code == 404

async def test_ingest_has_no_metadata_route(ingest):
    assert (await ingest.get(WELL_KNOWN)).status_code == 404

@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_metadata_body(oauth_http):
    r = await oauth_http.get(WELL_KNOWN)
    assert (r.status_code, r.json()) == (200, {"resource": RESOURCE, "authorization_servers": [ISSUER],
        "scopes_supported": ["webhooks-mcp"], "bearer_methods_supported": ["header"]})

@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_no_credentials_is_401_with_resource_metadata(oauth_http):
    r = await oauth_http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Host": "evil.example"})
    assert (r.status_code, r.json(), r.headers["www-authenticate"]) == (401, {"error": "unauthorized"}, NO_TOKEN)

@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
@pytest.mark.parametrize("value", ["Basic x", "Bearer ", "Bearer  t", "Bearer a b", "bearer KEY", "bearer TOKEN"])
async def test_malformed_header_is_401_without_error(oauth_http, issuer, value):   # Open question 5
    # KEY/TOKEN replaced by the static key / issuer.token() in the body
    ...assert (r.status_code, r.headers["www-authenticate"]) == (401, NO_TOKEN)

@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_non_ascii_header_is_401(oauth_http):                                # Review Focus 1
    r = await oauth_http.post("/mcp", json=INIT, headers=[*MCP_HEADERS.items(), (b"authorization", b"Bearer \xff\xfe")])
    assert r.status_code == 401

@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_two_authorization_headers_is_401(oauth_http, issuer):
    good = f"Bearer {issuer.token()}"
    r = await oauth_http.post("/mcp", json=INIT, headers=[*MCP_HEADERS.items(),
                              ("Authorization", good), ("Authorization", good)])
    assert r.status_code == 401

@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_refused_token_is_401_invalid_token(oauth_http, issuer):
    for t in (issuer.token(aud="https://wm.example/other"), "k" * 40):
        r = await oauth_http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {t}"})
        assert (r.status_code, r.headers["www-authenticate"]) == (401, BAD_TOKEN)

@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_valid_token_reaches_mcp_with_or_without_sub(oauth_http, issuer):
    for t in (issuer.token(), issuer.token(drop=("sub",))):
        r = await oauth_http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {t}"})
        assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"]

@pytest.mark.parametrize("oauth_http", MODES, indirect=True)
async def test_token_without_scope_is_403(oauth_http, issuer):
    for kw in ({"scp": ["offline_access"]}, {"drop": ("scp",)}, {"scp": "webhooks-mcp"}):
        r = await oauth_http.post("/mcp", json=INIT,
                                  headers={**MCP_HEADERS, "Authorization": f"Bearer {issuer.token(**kw)}"})
        assert (r.status_code, r.json(), r.headers["www-authenticate"]) == (
            403, {"error": "insufficient_scope"}, NO_SCOPE)

async def test_issuer_down_and_no_key_is_503(oauth_http, issuer):
    issuer.down = True
    r = await oauth_http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {issuer.token()}"})
    assert (r.status_code, r.json()) == (503, {"error": "issuer unavailable"}) and "www-authenticate" not in r.headers

@pytest.mark.parametrize("oauth_http", ["static+oauth"], indirect=True)
async def test_static_key_still_works_while_issuer_down(oauth_http, issuer):
    issuer.down = True
    r = await oauth_http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200 and issuer.requests == []

@pytest.mark.parametrize("mode", MODES)
async def test_default_verifier_never_contacts_the_issuer_at_start(store, tmp_path, mode):
    # Settings for `mode` with ISSUER/RESOURCE, build_mcp_app(store, settings) without a verifier, served via serve():
    # POST /mcp without credentials → 401 NO_TOKEN; GET WELL_KNOWN → 200; in static+oauth the key → 200

async def test_refusals_log_mode_and_reason_only(oauth_http, issuer, clock, caplog):
    caplog.set_level(logging.INFO, logger="webhooks_mcp")
    bad = issuer.token(aud="https://x.example", sub="sub-secret-7")
    unscoped = issuer.token(scp=["other"], sub="sub-secret-7")
    # POST without credentials, with `bad`, with `unscoped`; then issuer.down on a fresh kid
    # (issuer.token(header={"kid": "k9"})) after advancing the clock past JWKS_REFETCH_MIN_SECONDS → 401
    # (the cached set is within JWKS_MAX_AGE_SECONDS)
    assert [r.getMessage() for r in caplog.records if r.name == "webhooks_mcp.mcp_app"] == [
        "mcp auth refused: mode=oauth reason=missing", "mcp auth refused: mode=oauth reason=invalid",
        "mcp auth refused: mode=oauth reason=scope", "mcp auth refused: mode=oauth reason=invalid"]
    assert not any(s in caplog.text for s in (bad, unscoped, KEY, "sub-secret-7"))

async def test_expired_key_set_with_issuer_down_is_503(oauth_http, issuer, clock):   # Open question 3: answered no
    ok = await oauth_http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {issuer.token()}"})
    assert ok.status_code == 200
    clock.advance(timedelta(seconds=JWKS_MAX_AGE_SECONDS + 1)); issuer.down = True
    r = await oauth_http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {issuer.token()}"})
    assert (r.status_code, r.json()) == (503, {"error": "issuer unavailable"}) and "www-authenticate" not in r.headers

async def test_issuer_unavailable_is_logged(oauth_http, issuer, caplog):
    # issuer.down, no key cached → 503 and one message "mcp auth refused: mode=oauth reason=issuer_unavailable"

async def test_static_mode_logs_no_refusal(mcp_http, caplog):
    caplog.set_level(logging.DEBUG)
    await mcp_http.post("/mcp", json=INIT, headers=MCP_HEADERS)
    assert not [r for r in caplog.records if r.name == "webhooks_mcp.mcp_app"]
```

The existing `tests/test_mcp_app.py` auth tests (`static`: `Bearer` header, lowercase `bearer`, wrong key) stay
unchanged and must keep passing: that is the "static unchanged" pin.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_mcp_auth.py -v`
Expected: FAIL (`build_mcp_app` takes no `verifier`; there is no metadata route; every OAuth case gets the static 401).

- [ ] **Step 3: Implement** in `src/webhooks_mcp/mcp_app.py`.

`McpEndpoint.__call__` follows spec § 6.7 steps 1–4. In `static` it keeps today's code path and adds no log.
In the OAuth modes:
1. Two or more `authorization` entries in `scope["headers"]` → 401 `NO_TOKEN`, reason `invalid`.
2. In `static+oauth`, a `compare_digest` match on the single value → dispatch.
3. No header → 401 `NO_TOKEN`, reason `missing`.
4. A value that does not start with `b"Bearer "`, does not decode as ASCII, or whose remainder is empty or contains
   whitespace (`re.fullmatch(rb"\S+", ...)`) → 401 `NO_TOKEN`, reason `invalid`.
5. `verify_token`: `IssuerUnavailable` → 503, reason `issuer_unavailable`; `None` → 401 `BAD_TOKEN`, reason `invalid`;
   `REQUIRED_SCOPE not in scopes` → 403 `NO_SCOPE`, reason `scope`; otherwise dispatch.

Log `log.info("mcp auth refused: mode=%s reason=%s", mode, reason)` with
`log = logging.getLogger(__name__)`. Compute the metadata URL once:
`str(build_resource_metadata_url(settings.oauth_resource))`. In the OAuth modes, append
`create_protected_resource_routes(resource_url=settings.oauth_resource, authorization_servers=[settings.oauth_issuer],
scopes_supported=[REQUIRED_SCOPE])` to the routes, passing the strings as configured. The lifespan awaits
`verifier.aclose()` after `session_manager.run()` exits.

- [ ] **Step 4: Run them to verify they pass**

Run: `uv run pytest tests/test_mcp_auth.py tests/test_mcp_app.py tests/test_main.py -W error -v`
Expected: PASS.

- [ ] **Step 5: Update the docs** (Teardown in `CLAUDE.md`).
  - `README.md`: in the opening table and "MCP tools", say the auth depends on `AUTH_MODE`, with a three-row table copied
    from spec § 6.7. In "Configuration", add `AUTH_MODE`, `OAUTH_ISSUER` and `OAUTH_RESOURCE` with the § 6.5 wording,
    and make `MCP_AUTH_KEY` "required in `static` and `static+oauth`, refused in `oauth`". In "Security model", add
    the OAuth bullet: issuer + audience + `webhooks-mcp` scope, no subject list, tokens never logged.
  - `docs/contracts.md` C1 and C4: delete ", not built" from "(spec 6.7, not built; …)" and "(spec 6.5, 6.7, not built)".
    No value changes.
  - Spec § 6.7 heading: `(amended 2026-11-24 and 2026-11-27, not built)` → `(amended 2026-11-24 and 2026-11-27)`.

- [ ] **Step 6: Run the whole check** (the CI invocation, then the image)

Run: `uv lock --check && uv run ruff check . && uv run ruff format --check . && uv run pytest -W error`
Expected: no lock drift, no lint findings, no files to reformat, and every test passes (the 2 root-only skips stay skips).

Run: `docker build -t webhooks-mcp:dev . && scripts/smoke.sh webhooks-mcp:dev`
Expected: the last line is `smoke: ok`. Smoke runs `static` unchanged; this proves the image still builds, starts
read-only and serves with the new direct dependencies (Open question 2).

- [ ] **Step 7: Commit**

```bash
git add src/webhooks_mcp/mcp_app.py tests/oauth_fixtures.py tests/test_mcp_auth.py README.md docs/contracts.md \
  docs/superpowers/specs/2026-11-11-webhooks-mcp-design.md
git commit -m "feat(mcp): AUTH_MODE static+oauth and oauth on POST /mcp, protected resource metadata"
```

The report says the image content changed (`src/`, `pyproject.toml`, `uv.lock`). The edge still needs the bump PR
merged and a deploy (`docs/release.md`); nothing changes there until the edge sets `AUTH_MODE` (sso.md § 6b).

---

## Open questions for the owner

Execution waits on the open ones (1, 2, 4, 5, 6). The plan's provisional answer is in brackets; the tests that pin it
are marked.

1. **Is an env var set to the empty string "set"?** This covers `MCP_AUTH_KEY=""` in `oauth`, `OAUTH_*=""` in `static`
   and `AUTH_MODE=""`. § 6.7 says "set in `oauth`" and "set in `static`" without saying whether that means present or
   non-empty. *[Present counts: all three are refused at start. T1.]*
2. **Should `scripts/smoke.sh` cover an OAuth mode?** § 6.7 asks for no smoke change. Running the image in `oauth` with
   a dummy issuer, and checking `POST /mcp` → 401 with `resource_metadata` plus the metadata body, would prove the
   Gatus-facing contract (C1) in the real container. *[No: smoke runs unchanged as the image check. T4 step 6.]*
3. **Stale keys after `JWKS_MAX_AGE_SECONDS`.** *Answered no (owner, 2026-11-27; SSO spec § 3 "Signing keys", spec
   § 6.7 Keys):* a key from a set older than `JWKS_MAX_AGE_SECONDS` is never used. When the refetch fails, times out
   or the limit blocks it, every token that needs a key gets `IssuerUnavailable` (503), including one whose `kid` is in
   the old set. T2 `test_expired_set_with_refetch_failing_raises`, `test_expired_set_with_refetch_rate_limited_raises`;
   T4 `test_expired_key_set_with_issuer_down_is_503`.
4. **A JWKS that parses but holds no usable key** (empty `keys`, or keys without a `kid`). § 6.7 lists timeout,
   non-200, oversized and unparsable as failed fetches, not this case. *[It is a failed fetch, so the old set is
   kept, and used only while it is within `JWKS_MAX_AGE_SECONDS` (question 3). T2.]*
5. **`WWW-Authenticate` and the log reason for a malformed header in an OAuth mode** (`Basic …`, `Bearer ` with
   nothing after it, `bearer <token>`, two `Authorization` headers). The § 6.7 table adds `error="invalid_token"`
   only "for a presented, refused token". *[No `error=` parameter; log reason `invalid`. A well-formed `Bearer
   <x>` that fails verification, including a wrong static key in `static+oauth`, gets `error="invalid_token"`. T4.]*
6. **Revocation.** *Resolved (owner, 2026-11-27):* SSO spec § 4.11 chose C (5-minute access tokens for the Claude.ai
   client, revocation through `sso_people`); spike S27 showed Authelia's bearer check accepts only opaque tokens, so A
   was rejected. Nothing changes here: the plan builds the JWT verifier as specified.
