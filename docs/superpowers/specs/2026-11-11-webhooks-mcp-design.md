# webhooks-mcp — design

Date: 2026-11-11. Status: approved by the owner; implemented and deployed (2026-11-12). Section 6 is the
rationale for the listener's behaviour; the frozen contract values live in `docs/contracts.md`.

Amended 2026-11-24 for single sign-on (6.7): **specified, not built.** The source of truth for the shared design is
`davcs86/homelab-edge` `docs/superpowers/specs/2026-11-23-authelia-sso-respec.md` (decisions 4, 5, 16; § 4.6; § 5;
phase P4). Decision 16 changed on 2026-11-24 (owner): the service authorizes by issuer, audience and the `webhooks-mcp`
scope only, with no `(iss, sub)` allowlist (SSO spec decision 16 as amended). Amended 2026-11-27 (owner, SSO spec
decisions 16 and 21 as amended, § 3 "Signing keys", § 4.11): no token is verified on a key set older than
`JWKS_MAX_AGE_SECONDS`, and the edge registers one client, Claude.ai's. Until an image with 6.7 ships
and the edge sets `AUTH_MODE`, the service behaves exactly as below.

Spans three repositories: `davcs86/webhooks-mcp` (the service), `davcs86/homelab-edge` (its deployment
on the edge droplet), `davcs86/xstockstrat` (the first publisher).

## 1. Intent

Web-based AI agents such as Claude.ai can only pull: nothing can call them back. Closed services such
as xstockstrat finish long-running work (a backfill) minutes after the agent asked for it, and today the
agent has to poll a status tool.

`webhooks-mcp` is a small mailbox. Services POST webhooks to it; agents read the mailbox through MCP
tools. It is deliberately generic — xstockstrat is the first publisher, not the only one intended.

## 2. Goals, non-goals, anti-goals

**Goals**

- Accept a webhook over HTTP, persist it, return an `event_id`.
- Let an agent query received webhooks over MCP by `event_name` and by `event_id`, and wait briefly for
  the next one.
- Let an agent mint a unique webhook URL over MCP and hand it to a publisher.
- xstockstrat delivers backfill terminal events at-least-once to a `callback_url` given on
  `trigger_backfill`.

**Non-goals (decided, not forgotten)**

- ~~OAuth / OIDC on the MCP side.~~ Amended 2026-11-24: the MCP side becomes an OAuth 2.1 resource server (6.7);
  the service still never issues tokens, hosts a login or registers clients.
- Multiple users or tenant isolation. Single owner: every caller holding a valid token with the `webhooks-mcp` scope
  (6.7) sees the whole store.
- Publishers outside the tailnet. Ingestion is tailnet-only, so the DigitalOcean-hosted xstockstrat apps
  and outside SaaS cannot publish, and a minted URL only works for a tailnet caller.
- Authentication or signing on ingestion. Tailnet reachability is the control.
- Push to agents (SSE notifications, email). Pull only.
- Backups of received events. They are transient.
- OpenTelemetry in the listener. Structured stdout logs only.

**Anti-goals — the design has failed if any of these is true**

- The ingestion port is reachable from the public internet.
- The MCP bearer key or an access token appears in a log, a commit, or the repo.
- A token from another issuer, for another audience, or without the `webhooks-mcp` scope reads an event (6.7).
- A webhook payload is presented to an agent as anything other than untrusted data.
- A lost or duplicated webhook delivery goes unnoticed (no retry, no dedup, no dead-letter signal).

## 3. Constraints found in the code

| Constraint | Source |
|---|---|
| Public surface is exactly 80/443/8443 tcp + 3478 udp; a new service rides Caddy by hostname | `homelab-edge/CLAUDE.md` rule 2 |
| A new public hostname touches five hardcoded places: Caddyfile, ACME CNAME, `tls-check`, Gatus, split-horizon names | `docs/runbooks/onboard-service.md` §4 |
| Edge has no database; `tag:edge` is denied the shared Postgres; droplet is 1 vCPU / 2 GB | `policy/policy.hujson`, `group_vars/all/main.yml` |
| Homelab prod reaches the edge on 443, homelab staging only on 8443 — both grants already exist | `policy/policy.hujson` |
| Every image pinned by digest with a rationale in `docs/versions.md`; generated secrets via `init-secrets` `fill_missing` | `homelab-edge/CLAUDE.md` rules 3–4, `docs/decisions.md` |
| xstockstrat has no generic outbound webhook; notify's Slack/SendGrid fanout is best-effort (no retry, no delivery log) | `notify/src/fanout/fanout.ts` |
| `trigger_backfill` is the async job agents poll; a PARTIAL job reports as `ingest.backfill.completed` | `ingest/app/handlers/servicer.py` |
| New xstockstrat capability must go through the SDD pipeline; changing `trigger_backfill` must update the `strat-lab` `backtest` skill in the same PR | `xstockstrat/CLAUDE.md` |

## 4. Approaches considered

**Listener.** One shape survived; the alternatives were ruled out by owner decisions: OAuth server
inside the service (deferred to shared OIDC), delegating auth to xstockstrat identity (not standalone),
Postgres (unreachable from the edge), code living in `homelab-edge` (owner chose its own repo + GHCR).

**xstockstrat publisher.** Where delivery lives:

| | Approach | Trade-off |
|---|---|---|
| A | Delivery inside `xstockstrat-ingest` | Smallest; backfill-only, the next publisher re-implements retries |
| **B** | **Generic delivery in `xstockstrat-notify`** | **Built once; any service gets webhooks by calling one RPC. Notify already owns outbound HTTP and a direct DB connection** |
| C | New `xstockstrat-webhooks` service | Cleanest isolation; costs a 14th image, a DB pool slot, an mTLS cert, platform-lead approval |

**Owner picked B.**

How events reach notify, within B:

| | Source | Trade-off |
|---|---|---|
| B1 | Notify tails ledger `StreamEvents` for events stamped with a `callback_url` | Publisher only stamps a field; but adds a notify → ledger edge (a dependency loop), makes notify read every ledger event, and inherits ingest's best-effort ledger write |
| **B2** | **Publisher calls a dedicated notify RPC, `FanoutWebhook`; notify queues and delivers** | **No ledger involvement; the publisher must retry the hand-off itself** |
| B3 | Reuse `EmitAlert` | Every webhook would land in alert history, the UI stream and push fanout; a clean backfill emits no alert today |

**Owner picked B2**, with the hand-off from ingest queued and retried (section 8).

## 5. Owner decisions

1. MCP auth: static secret in a request header, single owner. → `Authorization: Bearer <key>`.
   Claude.ai sends it through the connector's "Request headers" option, which is in beta; the owner
   accepted that dependency. Amended 2026-11-24 (owner, homelab-edge SSO spec decisions 5 and 16): `AUTH_MODE`
   `static` (this decision, the default) | `static+oauth` (transition) | `oauth`; in the OAuth modes the service
   accepts JWT access tokens from one external issuer and authorizes by issuer, audience and scope only (6.7).
2. `event_name` is primarily a shared event type; minted URLs get their own unique name.
3. Ingestion: no authentication, behind Caddy, tailnet-only.
4. Pull-only for agents.
5. Delivery in notify (approach B) through a dedicated `FanoutWebhook` RPC — not a ledger tail, to
   avoid a notify ↔ ledger loop. `callback_url` on backfills, in scope. Ingest queues the hand-off and
   retries it until notify acknowledges.
6. MCP is public; ingestion is internal.
7. SQLite, replayable reads, no backup; TTL is a named constant.
8. Own repo `davcs86/webhooks-mcp`, public GHCR image pinned by digest, Python.
9. Light schema: `event_name` + schemaless `payload`.
10. Hostnames live under the homelab domain.

## 6. The listener (`davcs86/webhooks-mcp`)

Items marked **(call)** are the designer's choices, not owner decisions — correct any of them.

### 6.1 Process

One Python 3.13 container (uv, MCP Python SDK v2 `MCPServer`, Starlette, uvicorn — same stack as
`xstockstrat-agent`). One process, **two listeners** **(call)**:

| Listener | Container port | Serves |
|---|---|---|
| MCP | 9100 | `POST /mcp` (Streamable HTTP), `GET /healthz` |
| Ingest | 9101 | `POST /hooks`, `POST /hooks/{name}`, `GET /healthz` |

Two ports so that Caddy's public site can only ever proxy to the MCP port; a routing mistake cannot
expose ingestion. Single process because `wait_for_event` wakes on an in-process signal.

### 6.2 Ingestion contract

- **Typed:** `POST /hooks`, body `{"event_name": "<name>", "payload": <any JSON>}`.
- **Minted:** `POST /hooks/{name}`; the name comes from the path and the **whole body** is stored as the
  payload. This lets a publisher that knows nothing about this service post whatever it posts.
- `event_name`: `^[a-z0-9][a-z0-9._-]{0,127}$`. Minted names are `minted.<label>-<random>` **(call)**;
  the typed endpoint rejects the `minted.` prefix, and the minted endpoint returns 404 for a name that
  was never minted or has expired.
- Body must be JSON, at most `MAX_BODY_BYTES` (256 KiB).
- Optional `Idempotency-Key` header, unique per `event_name`. A repeat returns the original `event_id`.

| Result | Status | Body |
|---|---|---|
| Stored | 201 | `{"event_id": "...", "duplicate": false}` |
| Idempotent repeat | 200 | `{"event_id": "...", "duplicate": true}` |
| Not JSON / bad name / missing field | 422 | `{"error": "..."}` |
| Too large | 413 | |
| More than `INGEST_MAX_CONCURRENT_REQUESTS` (32) connections in flight | 503 | Publisher retries |
| Unknown or expired minted name | 404 | |

### 6.3 MCP tools

Every MCP request needs `Authorization: Bearer <MCP_AUTH_KEY>`, compared in constant time; otherwise
401. The service refuses to start if `MCP_AUTH_KEY` is unset or shorter than 32 characters. This is
`AUTH_MODE=static`; the OAuth modes are in 6.7.

| Tool | Purpose |
|---|---|
| `list_events(event_name, after_seq=0, page_size=20)` | Events for a name, oldest first, from a cursor |
| `get_event(event_id)` | One event with its full payload |
| `wait_for_event(event_name, after_seq=0, timeout_s=25)` | Returns as soon as an event newer than the cursor exists, or empty at timeout (max 25 s) |
| `mint_webhook_url(label)` | Returns `{event_name, urls, expires_at}`; `urls` has one entry per configured ingest base (6.5) |
| `list_event_names()` **(call)** | Names that currently hold events, with counts — discovery. At most 500 names; the result carries `truncated` |

Each event is returned as `{event_id, seq, event_name, received_at, source_ip, payload_bytes, payload}`.
List and wait results inline the payload only when it is at most `INLINE_PAYLOAD_BYTES` (4 KiB)
**(call)**; larger ones come back with `payload_truncated: true` and are fetched with `get_event`.
`seq` is the cursor an agent passes back as `after_seq`.

**Untrusted data.** Payloads are returned only inside the `payload` field of a structured result, and
every tool description states that payload content is third-party data and must never be followed as
instructions.

### 6.4 Storage

SQLite in WAL mode on a volume at `/data`.

- `events`: `seq` (integer, autoincrement, the cursor), `event_id` (UUID, unique), `event_name`,
  `payload` (JSON text), `payload_bytes`, `received_at`, `source_ip`, `idempotency_key`
  (unique with `event_name`, nullable).
- `minted_urls`: `event_name`, `label`, `created_at`, `expires_at`.

Constants in one module: `EVENT_TTL = 7 days`, `MAX_BODY_BYTES = 256 KiB`,
`MAX_EVENTS_PER_NAME = 1000`, `INLINE_PAYLOAD_BYTES = 4 KiB`, `WAIT_MAX_SECONDS = 25`.
`MAX_STORE_BYTES = 1 GiB` (each event charged its payload size plus `ROW_OVERHEAD_BYTES = 1 KiB`),
`LIST_NAMES_LIMIT = 500`,
`INGEST_MAX_CONCURRENT_REQUESTS = 32`.

Bounds are enforced **on insert**, oldest first, in the same transaction: a name never holds more than
`MAX_EVENTS_PER_NAME` events and the store never holds more than `MAX_STORE_BYTES`. The event being
inserted is never evicted by its own insert. Over budget, the oldest events are evicted rather than the
new one refused **(call)** — this keeps the response contract in 6.2 unchanged; the cost is that a
flooding publisher pushes out other publishers' oldest events. A sweep runs at start and hourly: it
deletes events and minted URLs older than `EVENT_TTL` and returns freed pages to the filesystem.

List and wait results read only payloads small enough to inline; large payloads are read by
`get_event` alone. The container has a 192 MB memory limit, and parsing a page of large payloads to
discard them exceeded it. A minted URL lives for `EVENT_TTL` **(call)**.

Reads never delete: an event can be read any number of times until it expires.

### 6.5 Configuration

| Env var | Meaning |
|---|---|
| `MCP_AUTH_KEY` | The bearer key (secret). Required in `static` and `static+oauth`; refused at start in `oauth` (6.7) |
| `AUTH_MODE` | `static` (default) \| `static+oauth` \| `oauth` (6.7); any other value is refused at start |
| `OAUTH_ISSUER` | Required in the OAuth modes, refused otherwise: the token issuer, compared to `iss` as an exact string; `https:`, no trailing slash, query or fragment. On the edge: `https://auth.public.<domain>` |
| `OAUTH_RESOURCE` | Required in the OAuth modes, refused otherwise: this server's resource URL, the expected `aud` and the protected resource metadata's `resource`; `https:`, path exactly `/mcp`, no query or fragment. On the edge: `https://webhooks-mcp.public.<domain>/mcp` |
| `INGEST_BASE_URLS` | JSON map of label → base URL used to build minted URLs **(call)**. On the edge: `{"default": "https://webhooks.private.<domain>"}`. The map can hold several bases when publishers reach the edge on different addresses (staging once needed its own, on 8443); the agent passes the entry that matches the publisher |
| `DATA_DIR` | Defaults to `/data` |

### 6.6 Build and release

- `Dockerfile`: `python:3.13-slim` pinned by digest, `uv sync --frozen`, non-root user, works with a
  read-only root filesystem (only `/data` writable).
- CI on every PR: `ruff`, `pytest`, image build. On `main`: push `ghcr.io/davcs86/webhooks-mcp` and
  print the digest. The package is **public** (owner decision), so the droplet pulls it without a
  registry credential.
- Tests are written first and cover: the ingestion table in 6.2, idempotent repeat, minted
  lifecycle (mint → post → expire → 404), cursor pagination, `wait_for_event` waking on insert and
  timing out, TTL and per-name trimming, 401 without or with a wrong key, and the ingest routes being
  absent from the MCP port (and the reverse). The OAuth modes add the cases in 6.7, run against a stub issuer.

### 6.7 MCP authentication modes (amended 2026-11-24 and 2026-11-27, not built)

| `AUTH_MODE` | Accepted on `POST /mcp` | Protected resource metadata |
|---|---|---|
| `static` (default) | `Bearer <MCP_AUTH_KEY>` only: today's behaviour, unchanged | 404 |
| `static+oauth` | the static key, or an access token as below | served |
| `oauth` | an access token only | served |

The default keeps today's behaviour, so a new image changes nothing until the edge sets `AUTH_MODE` (homelab-edge SSO
spec § 3 "Upgrade safety"). Because `static` refuses any `OAUTH_*` variable at start (below), the edge sets `AUTH_MODE`
and the `OAUTH_*` variables in one change and removes them in one change. The service is a resource server only: it
never issues tokens, hosts a login or registers clients, and nothing in it depends on Authelia; any issuer that signs
RS256 JWT access tokens works.

**Who may call** (owner, 2026-11-24, 2026-11-27): the service authorizes by issuer, audience and the `webhooks-mcp`
scope only, and keeps no list of subjects. Who can get such a token is decided at the issuer: on the edge, membership of
Authelia's `webhooks-mcp` group for its one client, `webhooks-mcp-claude` (Claude.ai's connector; SSO spec decisions 16
and 21 as amended, § 4.6). Accepted residuals: an issuer client misconfigured to hold that scope and audience is not
caught here; and a person removed from the group keeps an issued access token until its `exp` (1 h on the edge, 5 min under SSO spec § 4.11 C; at once under A), and
keeps refreshing for as long as the issuer allows: Authelia v4.39.28 kept refreshing after removal from the group
(xstockstrat 345 spike (d), `context.md`; SSO spec S4, S24). The edge closes that gap without a change here (SSO spec
§ 4.11): by Authelia's bearer check in front of `/mcp` in `oauth` (A, provided S27 shows it accepts the JWT access
tokens this service verifies), or by 5-minute access tokens (C).

**Request check.** `McpEndpoint` stays the one place a request is accepted or refused **(call)**. It does not use the
SDK's `BearerAuthBackend`/`RequireAuthMiddleware`: that backend matches the scheme case-insensitively (WM-08 requires the
exact `b"Bearer "` compare), reads wall time for `exp` (tests inject the clock), cannot tell a refused token from an
unreachable issuer (no 503), and knows nothing of the static key. In order:

1. In the OAuth modes, a request with more than one `Authorization` header → 401 **(call)**. (`static` keeps today's
   `dict(scope["headers"])`, which takes the last one.)
2. In `static` and `static+oauth`, an exact constant-time compare against `b"Bearer " + MCP_AUTH_KEY` (WM-08); a match
   is accepted with no token parsing.
3. In the OAuth modes, the header value must be `Bearer ` (exact, WM-08) followed by a non-empty token with no
   whitespace, else 401. The token goes to a `JwksTokenVerifier` implementing the SDK's `TokenVerifier` protocol
   (`verify_token(token) -> AccessToken | None`). `None` always means a refused token → 401 `invalid_token`; when the
   token needs a key the verifier cannot get (Keys, below) it raises `IssuerUnavailable`, which `McpEndpoint` maps to
   503 **(call)**. It accepts only:
   - header `alg` `RS256` (anything else, `none` included, refused);
   - header `typ` `at+jwt` or `application/at+jwt`, compared case-insensitively (the two spellings RFC 9068 § 4
     names); absent or any other value refused. Authelia v4.39.28 sets `at+jwt` (xstockstrat 345 spike (b));
   - a `kid` found in the issuer's JWKS, and a valid signature;
   - claims `exp`, `iss` and `aud` present (PyJWT `options={"require": ["exp", "iss", "aud"]}`), so a token without
     `exp` never lives forever; `sub` is not required, since nothing authorizes on it **(call)**;
   - `iss` equal to `OAUTH_ISSUER`; `aud` equal to, or a list containing, `OAUTH_RESOURCE`;
   - `exp` in the future and `nbf`/`iat`, when present, not in the future, each within `TOKEN_CLOCK_SKEW_SECONDS`
     (constant, 60 **(call)**) by the injected clock. PyJWT's own `verify_exp`, `verify_nbf` and `verify_iat` are
     turned off: PyJWT reads wall time.

   The `AccessToken` carries `scopes` from `scp` when present (anything but a list of strings gives no scopes), else
   from the space-delimited `scope` string, else none (Authelia v4.39.28 sends `scp` as an array, xstockstrat 345 spike (b)); `expires_at` from `exp`,
   `resource` `OAUTH_RESOURCE`, `subject` from `sub` and `client_id` from the `client_id` claim (RFC 9068), each empty
   when absent; only `scopes` is read **(call)**.
4. Those `scopes` must contain `REQUIRED_SCOPE` (constant, `webhooks-mcp`) → else 403 `insufficient_scope`; a malformed
   `scp` is never rescued by a `scope` claim.
   This is the whole authorization: `sub` and `client_id` play no part in it.

`AuthSettings(issuer_url=OAUTH_ISSUER, resource_server_url=OAUTH_RESOURCE, required_scopes=[REQUIRED_SCOPE],
validate_token_resource=False)` is built standalone at start to validate the configuration and is not passed to
`MCPServer`, which would then require a `token_verifier` and wire the SDK's middleware **(call)**. The verifier checks
`aud` itself; setting the flag explicitly keeps the SDK's `MCPDeprecationWarning` out of `pytest -W error`.

**Responses** (WM-05: fixed reasons, never echoing input):

| Case | Status | `WWW-Authenticate` |
|---|---|---|
| No credentials, malformed header, more than one `Authorization` header (OAuth modes), wrong static key, refused token | 401 `{"error": "unauthorized"}` | `Bearer` in `static`; in the OAuth modes `Bearer resource_metadata="<metadata URL>"`, plus `error="invalid_token"` for a presented, refused token |
| Valid token without the scope | 403 `{"error": "insufficient_scope"}` | `Bearer error="insufficient_scope", scope="webhooks-mcp", resource_metadata="<metadata URL>"` |
| A token that needs a key while no key set fetched within `JWKS_MAX_AGE_SECONDS` is cached and a fetch fails or is not allowed (`IssuerUnavailable`; Keys, below) | 503 `{"error": "issuer unavailable"}` **(call)** | — |

`<metadata URL>` is `build_resource_metadata_url(OAUTH_RESOURCE)`, computed once at start (on the edge
`https://webhooks-mcp.public.<domain>/.well-known/oauth-protected-resource/mcp`), never from the request's `Host`. The
homelab-edge Gatus probe ("POST `/mcp` without a key → 401") holds in every mode. A 503 rather than a 401 keeps a client
from discarding a good token while the issuer is down **(call)**.

**Protected resource metadata** (RFC 9728), on the MCP port only, in the OAuth modes: `GET
/.well-known/oauth-protected-resource/mcp`, built with the SDK's `create_protected_resource_routes(resource_url=
OAUTH_RESOURCE, authorization_servers=[OAUTH_ISSUER], scopes_supported=[REQUIRED_SCOPE])`, with both URLs passed as the
configured strings: a bare `AnyHttpUrl("https://auth.public.<domain>")` serialises with a trailing `/` and breaks
clients' exact issuer match. Unauthenticated; homelab-edge proxies that path on the public MCP site (`bearer_mcp`).

**Keys.** The verifier reads `jwks_uri` from `<OAUTH_ISSUER>/.well-known/openid-configuration`; a discovery document
whose `issuer` differs from `OAUTH_ISSUER`, or whose `jwks_uri` is not `https:` on the issuer's origin, is a failed
fetch **(call)**. The discovery document and the keys are fetched together, lazily by the first token that needs a key,
and replaced together by each successful fetch. A token is verified only with a key from a key set fetched within the
last `JWKS_MAX_AGE_SECONDS` (constant, 3600: one access-token lifetime, matching the edge's JWKS rotation, SSO spec
§ 4.7 "Rotation runbook"); no expired key is ever used (owner, 2026-11-27, SSO spec § 3 "Signing keys"). A token that
needs a key triggers a fetch when the cached set is older than that, or when its `kid` is not in the set. Every fetch
attempt (the first, the refresh after expiry, the one on an unknown `kid`, a retry after a failure) shares one limit, at
most one per `JWKS_REFETCH_MIN_SECONDS` (constant, 60) by the injected clock, so a stream of junk tokens on the public
MCP name cannot flood the issuer's back-channel zone. Concurrent requests wait on the one fetch in flight. Each request
gets `JWKS_FETCH_TIMEOUT_SECONDS` (constant, 5 **(call)**) and each response at most `JWKS_MAX_RESPONSE_BYTES`
(constant, 65536 **(call)**; WM-03); a timeout, a non-200, an oversized or an unparsable response is a failed fetch. A
failed fetch leaves the cached set as it was: usable until it is `JWKS_MAX_AGE_SECONDS` old, never after. After any
fetch the limit allows: a `kid` in a set within `JWKS_MAX_AGE_SECONDS` → its key; a set within that age without the
`kid` → 401; no set within that age (none fetched yet, or the cached one older) → `IssuerUnavailable` (503) when the
fetch fails, times out or is not allowed by the limit, for every token, including one whose `kid` is in the expired set.
Startup never needs the issuer, so `static+oauth` keeps serving the static key while it is down.

The HTTP client is `httpx2`'s `AsyncClient` (2.16.0 in `uv.lock`, which `mcp` already depends on). The change that
builds 6.7 adds `httpx2` and `pyjwt[crypto]` (2.15.1 in `uv.lock`, through `mcp`) to `[project].dependencies` and runs
`uv lock` in the same PR: no new package. `httpx` is in the `dev` extra only and absent from the runtime image. PyJWT's
`PyJWKClient` is not used: it fetches with synchronous `urllib` and caches on its own `time.monotonic()`, outside the
injected clock and the limits above. On the edge the container reaches the issuer by its public name through
`extra_hosts: ["auth.public.<domain>:host-gateway"]` (homelab-edge SSO spec § 4.2, S18).

**Configuration refusals at start** (exit 2, as today): an unknown `AUTH_MODE`; `MCP_AUTH_KEY` missing or short in
`static`/`static+oauth`, or set in `oauth` (a forgotten key must not linger) **(call)**; any `OAUTH_*` variable set in
`static` **(call)**, or missing in an OAuth mode; `OAUTH_ISSUER` not an `https:` URL, or with a trailing slash, a query
or a fragment (a path is allowed) **(call)**; `OAUTH_RESOURCE` not an `https:` URL whose path is exactly `/mcp`, with no
query or fragment. Both would otherwise start cleanly and fail every token: the `iss` compare is exact, and the metadata
route and the `aud` compare derive from `OAUTH_RESOURCE`, which the edge proxies only on `/mcp`.

**Logs.** In the OAuth modes, one line per refused request with the mode and an enumerated reason (`missing`,
`invalid`, `scope`, `issuer_unavailable`); `static` logs nothing new **(call)**. Never the key, a token, a claim value or
a `sub` (Rule 5) **(call)**.

**Transition** (P4): the edge sets `static+oauth` → the one consumer that sends `MCP_AUTH_KEY` today, the Claude.ai
connector, moves to `webhooks-mcp-claude` (the edge's access logs show no other, homelab-edge `sso.md` § 6b) → the edge
sets `oauth` and stops passing `MCP_AUTH_KEY`. The SOPS key is retired only after P4 passes, so until then going back to
`static+oauth` is a revert of that one edge change (the mode and the key together). Once the key is retired, no image
older than 6.7 can be redeployed: it refuses to start without `MCP_AUTH_KEY`.

**Tests** (written first, `pytest -W error`, no network): a stub issuer inside the test process (a generated RSA key,
its JWKS and discovery document served through an injected fetcher). Cases: every row of the mode table; each refusal
in step 3 (wrong `iss`, wrong or missing `aud`, missing `exp`, expired and not-yet-valid by `FakeClock`, `alg` `none`
and `HS256`, another `typ` and `typ` absent (the stub issuer sets `typ` explicitly: PyJWT defaults to `JWT`), unknown
`kid`, bad signature), scopes as an `scp` array and as a `scope` string accepted, neither and an `scp` of the wrong type
→ 403, and an `aud` list containing the resource accepted; two
`Authorization` headers → 401 in the OAuth modes; one refetch on an unknown `kid` and none within
`JWKS_REFETCH_MIN_SECONDS`, after a success or a failure; concurrent requests sharing one fetch; a discovery document
with another `issuer`, and a fetch timeout, each a failed fetch; 403 without the scope, and a token with the scope but
no `sub` accepted; 503 with the issuer down and no cached key, and the static key still accepted in `static+oauth`
meanwhile; 503 for a token whose `kid` is in a set older than `JWKS_MAX_AGE_SECONDS` when the refetch fails, and when
the limit blocks it;
`bearer <key>` (lower case) still 401 in every mode (WM-08); the metadata route's body (issuer without a
trailing `/`) and its 404 in `static`; the `WWW-Authenticate` values of the table; every configuration refusal above;
no token or key in captured logs.

## 7. Deployment (`davcs86/homelab-edge`)

**Hostnames (call — the owner said "same domain", the labels are proposals):**

- MCP, public: `webhooks-mcp.public.<domain>` on 443.
- Ingestion, tailnet-only: `webhooks.private.<domain>` on 443, for prod and staging alike.

Amended 2026-11-20 (owner approved; contract C8): the MCP was `webhooks-mcp.<domain>` and the ingest
`webhooks.ts.<domain>` on 443 (prod) and 8443 (staging). homelab-edge now keeps `*.ts` for machines and serves
internet services under `*.public` and tailnet-only ones under `*.private`. It admits the staging VM on the ingest
name alone, so one URL on 443 serves every environment; both ports reached the same listener and store. Neither
listener reads the Host header and the MCP key is not bound to a hostname, so the former names keep working while
the edge still serves them. The deployment steps below describe the original sites.

Changes:

1. **Pin** the image by digest in `versions.yml`, rationale in `docs/versions.md`.
2. **Compose service** `webhooks-mcp` with the `x-hardening` anchor, `read_only`, `mem_limit 192m`,
   a `webhooks-data` volume, ports published on `127.0.0.1` only, `env_file`.
3. **Secret** `webhooks_mcp_auth_key` added through `init-secrets` `fill_missing`, written to a 0600
   env file with `no_log`.
4. **Caddy**
   - New public site `webhooks-mcp.<domain>`: only `/mcp` is proxied (to the MCP port, with
     `flush_interval -1`, a body cap, and its **own** per-IP rate-limit zone — the existing `mcp` zone
     lives inside `(public_routes)` and matches only `/agent` paths); everything else is 404.
   - `webhooks.ts.<domain>` added to the existing tailnet-only `*.ts` site → ingest port, with a body cap
     matching `MAX_BODY_BYTES`.
   - The same host added to the `*.ts:8443` site, wrapped in `tailnet_only`. That site is otherwise
     open to the internet, so this is the one place the anti-goal can be broken; a routing test pins it.
   - Caddy passes the client address so the listener can record `source_ip`.
5. **The other hardcoded spots**: ACME CNAME for the new public name (a manual DNS step for the
   owner) and `tls-check`; split-horizon records for **both** names. The ingest name must resolve to
   the edge's tailnet address for tailnet clients, otherwise they arrive from a non-tailnet source and
   `tailnet_only` aborts them.
6. **Gatus**: public probe "POST `/mcp` without a key → 401" plus certificate expiry; tailnet probe
   ingest `/healthz` → 200.
7. **`scripts/caddy-routing-test.sh`** gains: public `/mcp` routed; any other public path 404;
   `/hooks` unreachable on the public site; ingest host aborts for a non-tailnet client on both 443
   and 8443; ingest host routed for a tailnet client on both.
8. **Docs**: `onboard-service.md` / `README.md` where behaviour they describe changes; one
   `docs/decisions.md` entry (first custom-code service on the edge; ingestion unauthenticated by
   decision, tailnet-only).

No policy change: `xstockstrat → edge:443` and `xstockstrat-staging → edge:8443` already exist.

One instance serves both environments, so prod and staging events share a store. The publisher puts
`environment` in every payload (section 8.3) so an agent can tell them apart.

## 8. The publisher (`davcs86/xstockstrat`)

This is a new capability, so it enters through `/sdd-story` → `/sdd-design quick` before code. This
section is the input to that pipeline, not a substitute for it. Governance it triggers: proto changes
(additive, in `ingest.proto` and `notify.proto`), new config keys, DB migrations in ingest and notify.
No new service, no new dependency edge (ingest → notify exists already), no ledger change.

Two queues give at-least-once end to end: ingest retries the hand-off until notify acknowledges; notify
retries the HTTP delivery until the listener acknowledges. Duplicates are collapsed by idempotency keys
at each hop.

```
ingest job row ──sweep──▶ notify.FanoutWebhook ──row──▶ delivery worker ──POST──▶ webhooks-mcp
 (enqueued flag)            (acks after persist)        (backoff, dead-letter)     (Idempotency-Key)
```

### 8.1 `callback_url` on backfills (ingest)

- `TriggerBackfillRequest` gains an optional `callback_url`; the agent tool `trigger_backfill` exposes
  it. The `strat-lab` `backtest` skill is updated in the same PR.
- Ingest stores it on the job, with a "webhook enqueued at" marker (migration).
- When a job with a `callback_url` reaches a terminal state, ingest calls `FanoutWebhook` and sets the
  marker on acknowledgement. A periodic sweep retries terminal jobs whose marker is unset, so notify
  being down when a backfill finishes delays the webhook rather than losing it.
- The hand-off must not fail or slow the backfill itself: a failed call is left to the sweep.
- Ledger events are unchanged — audit only, no `callback_url` stamped on them.

### 8.2 `FanoutWebhook` and delivery (notify)

New unary RPC on `NotifyService`:

| Field | Meaning |
|---|---|
| `callback_url` | Target |
| `event_name` | Dotted name, e.g. `ingest.backfill.completed` |
| `payload` | Struct — the publisher's data |
| `idempotency_key` | Unique per logical event; ingest sends `backfill:<job_id>:<status>` **(call)** |

1. **Accept.** Validate the URL (below), insert a delivery row unique on `idempotency_key`, then
   acknowledge. A repeat with a known key acknowledges without a second row. A rejected URL is
   `INVALID_ARGUMENT` — the publisher must not retry it.
2. **Deliver.** A worker POSTs pending rows with exponential backoff. 2xx is success. 408, 429, 5xx and
   network errors are retried; any other 4xx is dead at once. After the last attempt the row is marked
   dead and a WARNING alert is emitted, so a lost delivery is visible.
3. **One slow target must not block others** — retries are per row, not head-of-line.
4. **Survives restart.** Pending rows are picked up again on start; the queue is the table, not memory.

**URL rule.** `https` only, and the origin (host and port) must be on an allowlist held in a notify
config key. The agent supplies this URL, so the allowlist is the SSRF control; notify is the single
enforcement point, both on accept and on every delivery. The allowlist is empty by default, so the
DigitalOcean deployments reject every `callback_url`. Ingest surfaces the rejection: `trigger_backfill`
with a disallowed URL should fail up front rather than at job end — how ingest learns the verdict
early (a validate-only call, or a shared key) is left to the SDD design.

`FanoutWebhook` is callable by internal services only, like `EmitAlert`; whether it needs an
`x-internal-caller` allow-list is left to the SDD design.

Notify stays on its existing single direct DB connection; the pool budget table does not change. The
worker assumes a single notify instance, as today.

### 8.3 Wire format

```
POST <callback_url>
Content-Type: application/json
Idempotency-Key: <idempotency_key>

{
  "event_name": "ingest.backfill.completed",
  "payload": {
    "source_service": "xstockstrat-ingest",
    "environment": "production | staging",
    "occurred_at": "...",
    "data": { "job_id": "...", "status": "COMPLETED | PARTIAL | FAILED | CANCELED", ... }
  }
}
```

Notify builds the envelope (`source_service`, `environment`, `occurred_at`) around the publisher's
`payload`, which becomes `data` **(call)**. There is no "partial" event name: a PARTIAL job is
`ingest.backfill.completed` with `status: PARTIAL`, so `status` is required in the backfill data.
`user_id` and other identity fields are not sent. The same body works against both listener endpoints:
`/hooks` reads `event_name` from it, a minted URL stores it whole.

### 8.4 The agent's flow, end to end

1. Agent calls `mint_webhook_url("backfill")` on webhooks-mcp → `https://webhooks.private.<domain>/hooks/minted.backfill-x7k2…`.
2. Agent calls xstockstrat `trigger_backfill(..., callback_url=<that URL>)` → `job_id`.
3. Later, agent calls `wait_for_event("minted.backfill-x7k2…")` and gets the terminal event.

Or, without minting: pass `https://webhooks.private.<domain>/hooks` and query
`list_events("ingest.backfill.completed")`, matching on `payload.data.job_id`.

## 9. Order of work

1. `webhooks-mcp`: service, tests, CI, first image digest.
2. `homelab-edge`: deploy and verify (routing test, Gatus, a manual POST over the tailnet, Claude.ai
   connector added with the bearer header).
3. `xstockstrat`: SDD feature for `callback_url` + `FanoutWebhook`; verified on homelab staging against the live
   listener via 8443.

Each step is usable on its own: after step 2 any tailnet service can already publish.

## 10. Left to the builder

- Module layout, SQLite access library, how the two listeners are started in one process.
- Exact retry schedule and attempt count for notify's delivery worker and the interval of ingest's
  sweep (config keys, seeded by migration).
- Names of the new xstockstrat config keys, tables and the SDD slug.
- Caddy rate-limit values for the ingest host.

## 11. Resume state (as of 2026-11-11) — superseded

Historical: the session state before any code existed. Everything below has shipped; do not resume from it.

**Where things stand.** Design **approved by the owner (2026-11-11)**. Implementation plans are in
`docs/superpowers/plans/`: `2026-11-11-orchestration.md` (start there — it holds the status table and
the merge rules; no decision is open) and `2026-11-11-webhooks-mcp-listener.md`.
No code exists in any repo. Work branch in all three repos: `claude/webhook-listener-mcp-ltjc1r`
(xstockstrat PRs target `main-dev`, the other two `main`). This document is PR #1 of `webhooks-mcp`.

**Open owner decisions — do not guess these.**

1. ~~Relay source and hand-off durability~~ — **decided:** dedicated `FanoutWebhook` RPC in notify,
   no ledger tail; ingest queues the hand-off and a sweep retries it (section 8).
2. ~~GHCR package visibility~~ — **decided: public.**
3. Confirmation of the items marked **(call)**, the hostname labels in section 7 above all.

**Next steps after approval.** Write the implementation plan, then build in the order of section 9.
The xstockstrat part starts with `/sdd-story` → `/sdd-design quick`, not with code.

**Code landmarks found during recon** (verify before relying on them — line numbers drift).

| Repo | What | Where |
|---|---|---|
| homelab-edge | Compose services, `x-hardening` anchor | `roles/stack/templates/compose.yml.j2` |
| homelab-edge | Public routes, rate-limit zones, `(tailnet_only)`, `*.ts` and `*.ts:8443` sites | `roles/stack/templates/Caddyfile.j2` |
| homelab-edge | Hostnames, limits, droplet size | `inventories/prod/group_vars/all/main.yml` |
| homelab-edge | Image pins | `inventories/prod/group_vars/all/versions.yml`, `docs/versions.md` |
| homelab-edge | Env-file secrets, Caddy build, `tls-check` | `roles/stack/tasks/main.yml` |
| homelab-edge | Split-horizon records | `roles/tailscale/tasks/main.yml` |
| homelab-edge | Gatus probes | `roles/stack/templates/gatus-config.yaml.j2` |
| homelab-edge | Grants and their tests | `policy/policy.hujson`, `scripts/policy-test.sh` |
| homelab-edge | Routing assertions | `scripts/caddy-routing-test.sh` |
| xstockstrat | Backfill terminal transitions and the existing `EmitAlert` call to copy | `services/xstockstrat-ingest/app/handlers/servicer.py` |
| xstockstrat | Agent tools `trigger_backfill` / `get_backfill_status` | `services/xstockstrat-agent/app/tools.py` |
| xstockstrat | Existing best-effort fanout to copy patterns from | `services/xstockstrat-notify/src/fanout/` |
| xstockstrat | Protos to change | `packages/proto/ingest/v1/ingest.proto`, `packages/proto/notify/v1/notify.proto` |
| xstockstrat | MCP server stack to mirror | `services/xstockstrat-agent/app/main.py`, `Dockerfile` |
| xstockstrat | Skill that must change with `trigger_backfill` | `plugins/strat-lab/` (`backtest` skill) |

**Facts that are easy to lose.**

- Homelab staging reaches the edge only on 8443; 443 is denied by policy.
- The `*.ts:8443` Caddy site is open to the internet, unlike the `*.ts` 443 site.
- A Caddy route name must not equal a MagicDNS node name.
- A PARTIAL backfill emits `ingest.backfill.completed`; there is no partial event type.
- The ledger-tail relay was considered and rejected (section 4, B1); do not reintroduce a
  notify → ledger dependency.
- Notify's DB pool is 1 direct connection and must stay that way.
- The agent's `app/egress.py` blocks tailnet addresses; delivery lives in notify, so it is not affected.
