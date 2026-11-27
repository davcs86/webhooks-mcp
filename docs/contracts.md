# webhooks-mcp — frozen contracts

The cross-repo contracts between this service (`davcs86/webhooks-mcp`), its deployment
(`davcs86/homelab-edge`) and its first publisher (`davcs86/xstockstrat`). Changing any value here means:
amend the spec (`docs/superpowers/specs/2026-11-11-webhooks-mcp-design.md` §6), get the owner's approval, and
land the matching change in every consumer named in the last column. Never change one repo alone.

| # | Contract | Value | Producer → consumers |
|---|---|---|---|
| C1 | MCP listener | container port `9100`, `POST /mcp`, `GET /healthz`; `Authorization: Bearer <key>`; 401 without it. Amended 2026-11-24 (spec 6.7; the values before this sentence hold while `AUTH_MODE=static`, the default): `AUTH_MODE` `static` / `static+oauth` / `oauth`; in the OAuth modes also an RS256 JWT access token from `OAUTH_ISSUER` with `aud` `OAUTH_RESOURCE`, `GET /.well-known/oauth-protected-resource/mcp`, 401 with `WWW-Authenticate: Bearer resource_metadata="…"`, 403 for a missing `webhooks-mcp` scope, 503 when a token needs a key, the issuer's keys cannot be fetched and no key set fetched within the last 3600 s (`JWKS_MAX_AGE_SECONDS`) is cached (amended 2026-11-27: never an older key); 401 without credentials in every mode | webhooks-mcp → homelab-edge, the owner's MCP clients |
| C2 | Ingest listener | container port `9101`, `POST /hooks`, `POST /hooks/{name}`, `GET /healthz` | webhooks-mcp → homelab-edge, xstockstrat |
| C3 | Ingest responses | 201 `{event_id, duplicate:false}`, 200 `{…duplicate:true}`, 404, 413, 422, and 503 when over the concurrency limit — retry (spec 6.2) | webhooks-mcp → xstockstrat |
| C4 | Container env | `MCP_AUTH_KEY`, `INGEST_BASE_URLS` (JSON map), `DATA_DIR=/data`. Amended 2026-11-24 (spec 6.5, 6.7): `AUTH_MODE`, `OAUTH_ISSUER` (`https:`, no trailing slash, query or fragment), `OAUTH_RESOURCE` (path exactly `/mcp`); `MCP_AUTH_KEY` only in `static` and `static+oauth`; the edge sets `AUTH_MODE` and the `OAUTH_*` variables together and removes them together | webhooks-mcp → homelab-edge |
| C5 | Client address | Ingest reads the first address of `X-Forwarded-For` as `source_ip` | homelab-edge → webhooks-mcp |
| C6 | Body cap | 256 KiB at the listener; Caddy's cap on the ingest host equals it | webhooks-mcp ↔ homelab-edge |
| C7 | Image | `ghcr.io/davcs86/webhooks-mcp@sha256:<digest>`, public | webhooks-mcp → homelab-edge |
| C8 | Hostnames | MCP `webhooks-mcp.public.<domain>`; ingest `webhooks.private.<domain>` on 443, one URL for every environment. The former names stay served for a transition: MCP `webhooks-mcp.<domain>` until the MCP clients have been re-pointed; ingest `webhooks.ts.<domain>` on 443 and 8443 until the last URL minted under it has expired, then it is removed with the 8443 listener | homelab-edge → xstockstrat, owner |
| C9 | Wire format | spec 8.3: JSON envelope plus `Idempotency-Key` header | xstockstrat → webhooks-mcp |

Also part of the contract, enforced in code: the MCP tool names and argument names (`list_events`, `get_event`,
`wait_for_event`, `mint_webhook_url`, `list_event_names`; `src/webhooks_mcp/mcp_app.py#_build_server`), and the
untrusted-payload notice that ends every tool description (`src/webhooks_mcp/mcp_app.py#NOTICE`).
