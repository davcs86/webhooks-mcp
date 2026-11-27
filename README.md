# webhooks-mcp

A lite webhook server with an MCP interface. A publisher POSTs JSON to a webhook URL; the service
stores it; an AI agent then lists, fetches or waits for those events over MCP. Typical use: a
service in a private network hands an event to an agent that cannot be reached directly.

One process, two listeners:

| Listener | Port | Serves |
|---|---|---|
| MCP | 9100 | `POST /mcp` (Streamable HTTP, bearer key or OAuth access token per `AUTH_MODE`), `GET /healthz`; in the OAuth modes `GET /.well-known/oauth-protected-resource/mcp` |
| Ingest | 9101 | `POST /hooks`, `POST /hooks/{name}`, `GET /healthz` |

## Ingestion (port 9101)

Bodies must be JSON, at most 256 KiB. An optional `Idempotency-Key` header (unique per event name,
at most 255 characters) makes retries safe. The client address is the first entry of `X-Forwarded-For`
if valid, else the peer address.

```bash
# Typed: the name travels in the body
curl -X POST http://127.0.0.1:9101/hooks -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: build-42' \
  -d '{"event_name":"smoke.test","payload":{"ok":true}}'
# 201 {"event_id":"…","duplicate":false}      (a repeat: 200 with "duplicate":true)

# Minted: the name comes from the URL and the whole body is the payload
curl -X POST http://127.0.0.1:9101/hooks/minted.demo-k7q2xw4mzr3p -d '{"any":"json"}'
```

| Result | Status |
|---|---|
| Stored | 201 `{"event_id", "duplicate": false}` |
| Idempotent repeat | 200 `{"event_id", "duplicate": true}` |
| Not JSON, bad or missing `event_name`, missing `payload`, `minted.` prefix on the typed endpoint | 422 `{"error"}` |
| Body over 256 KiB | 413 |
| Minted name never issued or expired | 404 |
| More than 32 requests already in flight on the ingest listener | 503 (retry later) |

`event_name` matches `^[a-z0-9][a-z0-9._-]{0,127}$`.

## MCP tools (port 9100)

Every request needs `Authorization: Bearer <credential>`; which credentials are accepted depends on `AUTH_MODE`:

| `AUTH_MODE` | Accepted on `POST /mcp` | Protected resource metadata |
|---|---|---|
| `static` (default) | `Bearer <MCP_AUTH_KEY>` only | 404 |
| `static+oauth` | the static key, or an access token as below | served |
| `oauth` | an access token only | served |

An access token is an RS256 JWT (`typ` `at+jwt`) from `OAUTH_ISSUER`, with `aud` `OAUTH_RESOURCE` and the
`webhooks-mcp` scope. Without credentials, or with a refused one: 401 `{"error": "unauthorized"}`; in the OAuth modes
`WWW-Authenticate` names the metadata URL (`GET /.well-known/oauth-protected-resource/mcp`, RFC 9728). A valid token
without the scope: 403 `{"error": "insufficient_scope"}`. A token that needs a key while the issuer's keys cannot be
fetched and none fetched within the last hour are cached: 503 `{"error": "issuer unavailable"}`.

| Tool | Purpose |
|---|---|
| `list_events(event_name, after_seq=0, page_size=20)` | Events for a name, oldest first; `page_size` is clamped to 1..100. Returns `{events, next_after_seq}` |
| `get_event(event_id)` | One event with its full payload; `NOT_FOUND` if unknown |
| `wait_for_event(event_name, after_seq=0, timeout_s=25)` | Returns once an event newer than `after_seq` exists, or empty at the timeout (at most 25 s) |
| `mint_webhook_url(label)` | Creates a `minted.<label>-<random>` name; returns `{event_name, urls, expires_at}` with one URL per `INGEST_BASE_URLS` entry. `label`: lowercase letters, digits, hyphens, 1 to 32 characters |
| `list_event_names()` | Names that currently hold events, with counts, in name order. Returns `{names: [{event_name, count}], truncated}`; at most 500 names, `truncated: true` when more exist |

Each event is `{event_id, seq, event_name, received_at, source_ip, payload_bytes, payload}`. Lists and
waits inline a payload only up to 4 KiB; a larger one comes back as `payload_truncated: true` and is
fetched with `get_event`. Pass `next_after_seq` back as `after_seq` to page. Reads never delete.

## Configuration

| Env var | Meaning |
|---|---|
| `AUTH_MODE` | `static` (default) \| `static+oauth` \| `oauth`; any other value is refused at start |
| `MCP_AUTH_KEY` | Bearer key, at least 32 characters. Required in `static` and `static+oauth`, refused in `oauth` |
| `OAUTH_ISSUER` | Required in the OAuth modes, refused otherwise: the token issuer, compared to `iss` as an exact string; `https:`, no trailing slash, query or fragment |
| `OAUTH_RESOURCE` | Required in the OAuth modes, refused otherwise: this server's resource URL, the expected `aud` and the protected resource metadata's `resource`; `https:`, path exactly `/mcp`, no query or fragment |
| `INGEST_BASE_URLS` | JSON object of label to base URL used to build minted URLs; must contain `default`, e.g. `{"default":"http://127.0.0.1:9101"}` |
| `DATA_DIR` | Directory for the SQLite database (`events.db`). Default `/data`; must exist and be writable |

The process exits with status 2, naming the variable, on any refused configuration.

## Retention

Events and minted names live 7 days. Two bounds are enforced on every insert, evicting oldest
first: each event name keeps at most 1000 events, and the store as a whole stays within a 1 GiB
budget, deleting the globally oldest events until it fits. The budget charges each event its
`payload_bytes` plus a fixed 1 KiB per-row overhead, so many tiny events cannot outgrow it on disk. The event just
stored is never evicted by its own insert. Cursors stay valid: `seq` values are never reused, so an
evicted event simply no longer appears after `after_seq`. A sweep runs at start and hourly to expire
old events and minted names and to return freed space to the filesystem (for a database file first
created by this version; an older file keeps its free pages for reuse). These values live in
`src/webhooks_mcp/constants.py`.

## Run it

Locally (needs [uv](https://docs.astral.sh/uv/)):

```bash
mkdir -p /tmp/wm-data
DATA_DIR=/tmp/wm-data MCP_AUTH_KEY="$(openssl rand -hex 24)" \
  INGEST_BASE_URLS='{"default":"http://127.0.0.1:9101"}' uv run webhooks-mcp
```

With Docker, hardened the way the edge runs it:

```bash
docker run -d --name wm --read-only --cap-drop ALL --security-opt no-new-privileges --memory 192m \
  -v wm-data:/data -p 127.0.0.1:9100:9100 -p 127.0.0.1:9101:9101 \
  -e MCP_AUTH_KEY="$(openssl rand -hex 24)" \
  -e INGEST_BASE_URLS='{"default":"http://127.0.0.1:9101"}' \
  ghcr.io/davcs86/webhooks-mcp@sha256:<digest>
```

`scripts/smoke.sh <image>` runs this and checks the contract end to end. Tests: `uv lock --check && uv run pytest -W error` (as CI runs them).

## Security model

- **MCP**: bearer key compared in constant time; the key is never logged. The MCP port is the only one
  that may be exposed beyond a private network, behind TLS.
- **OAuth** (`static+oauth`, `oauth`): a token is accepted by issuer, audience and the `webhooks-mcp` scope only; there
  is no subject list, so who may get such a token is decided at the issuer. Tokens and their claims are never logged;
  a refusal logs only the mode and a fixed reason. Startup never contacts the issuer.
- **Ingestion has no authentication, by design.** Publish port 9101 only to a private network such as
  a tailnet. Minted names carry a random suffix and expire, but they are not secrets worth relying on.
- **Payloads are untrusted third-party data.** They are returned only inside a `payload` field, and
  every tool description tells the agent never to follow instructions found in one.
- The container runs as uid 10001 with a read-only root; only `/data` is written.

## Release

Merging to `main` publishes `ghcr.io/davcs86/webhooks-mcp:<sha>`, moves the tag `main` to it, and prints the digest
(`ghcr.io/davcs86/webhooks-mcp@sha256:…`) in the CI job summary. Deployments pin that digest, so a merge here changes
nothing until the pin is bumped where the service is deployed. The full path to the homelab edge, and the one-time
GHCR visibility step, are in `docs/release.md`. The cross-repo contracts are in `docs/contracts.md`.

MIT licensed.
