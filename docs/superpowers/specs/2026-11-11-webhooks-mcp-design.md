# webhooks-mcp — design

Date: 2026-11-11. Status: awaiting owner approval.

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

- OAuth / OIDC on the MCP side. Shared OIDC is planned later; a static bearer key is used until then.
- Multiple users or tenant isolation. Single owner.
- Publishers outside the tailnet. Ingestion is tailnet-only, so the DigitalOcean-hosted xstockstrat apps
  and outside SaaS cannot publish, and a minted URL only works for a tailnet caller.
- Authentication or signing on ingestion. Tailnet reachability is the control.
- Push to agents (SSE notifications, email). Pull only.
- Backups of received events. They are transient.
- OpenTelemetry in the listener. Structured stdout logs only.

**Anti-goals — the design has failed if any of these is true**

- The ingestion port is reachable from the public internet.
- The MCP bearer key appears in a log, a commit, or the repo.
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
   accepted that dependency.
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
| Unknown or expired minted name | 404 | |

### 6.3 MCP tools

Every MCP request needs `Authorization: Bearer <MCP_AUTH_KEY>`, compared in constant time; otherwise
401. The service refuses to start if `MCP_AUTH_KEY` is unset or shorter than 32 characters.

| Tool | Purpose |
|---|---|
| `list_events(event_name, after_seq=0, limit=20)` | Events for a name, oldest first, from a cursor |
| `get_event(event_id)` | One event with its full payload |
| `wait_for_event(event_name, after_seq=0, timeout_s=25)` | Returns as soon as an event newer than the cursor exists, or empty at timeout (max 25 s) |
| `mint_webhook_url(label)` | Returns `{event_name, urls, expires_at}`; `urls` has one entry per configured ingest base (6.5) |
| `list_event_names()` **(call)** | Names that currently hold events, with counts — discovery |

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
A sweep runs at start and hourly: it deletes events and minted URLs older than `EVENT_TTL` and trims
each name to `MAX_EVENTS_PER_NAME`, oldest first. A minted URL lives for `EVENT_TTL` **(call)**.

Reads never delete: an event can be read any number of times until it expires.

### 6.5 Configuration

| Env var | Meaning |
|---|---|
| `MCP_AUTH_KEY` | The bearer key (secret) |
| `INGEST_BASE_URLS` | JSON map of label → base URL used to build minted URLs **(call)**. On the edge: `{"default": "https://webhooks.ts.<domain>", "staging": "https://webhooks.ts.<domain>:8443"}`. Staging can only reach the edge on 8443, so one base is not enough; the agent passes the entry that matches the publisher |
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
  absent from the MCP port (and the reverse).

## 7. Deployment (`davcs86/homelab-edge`)

**Hostnames (call — the owner said "same domain", the labels are proposals):**

- MCP, public: `webhooks-mcp.<domain>` on 443.
- Ingestion, tailnet-only: `webhooks.ts.<domain>` on 443 (prod) and on 8443 (staging).

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

1. Agent calls `mint_webhook_url("backfill")` on webhooks-mcp → `https://webhooks.ts.<domain>/hooks/minted.backfill-x7k2…`.
2. Agent calls xstockstrat `trigger_backfill(..., callback_url=<that URL>)` → `job_id`.
3. Later, agent calls `wait_for_event("minted.backfill-x7k2…")` and gets the terminal event.

Or, without minting: pass `https://webhooks.ts.<domain>/hooks` and query
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

## 11. Resume state (as of 2026-11-11)

Read this section first when picking the work up in a fresh context.

**Where things stand.** Design written and reviewed once against the code; awaiting owner approval.
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
