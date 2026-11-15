# webhooks-mcp — Constitution

Derived by `/context-constitution` (context-forge) on 2026-11-14; last refreshed 2026-11-15 from branch
`claude/context-constitution-parity-zggeq3` at commit `1a7b63c` — the repo state this analysis reflects; a
later `refresh` reruns against a newer commit. This file captures the **non-obvious**: patterns this repo
follows but never wrote down, the places that break those patterns, the contracts with
`davcs86/homelab-edge` and `davcs86/xstockstrat`, and the scars behind them. These are the things an agent
would otherwise miss and get wrong. It does **not** restate `CLAUDE.md`, `docs/contracts.md` or CI (see
`## Pointers`).

> **ID scheme (CF-N5).** `docs/contracts.md` uses `C1`–`C9` for cross-repo wire contracts, which are a
> different kind of rule. The codebase invariants below use the sibling namespace `WM-*` and cite `C*`
> where they touch a contract.

## Floor (`WM-*`) — never-do, non-overridable

| ID | Rule | Why | Evidence |
|---|---|---|---|
| **WM-01** | Every DB access, **reads included**, goes through `EventStore._run`, which holds `_write_lock` around the one shared `check_same_thread=False` connection. Never call `self._db` or `asyncio.to_thread(... self._db.execute ...)` directly. The only exceptions are the lifecycle edges: `open` (before `_db` is published) and `close` (takes `_write_lock` itself). | `CLAUDE.md` calls it "the write lock", which invites an unlocked read. Two threads on one sqlite3 connection cause intermittent errors and cursor corruption. | `src/webhooks_mcp/store.py#_run` (authoritative); callers `#insert`, `#get`, `#list`, `#names`, `#mint`, `#is_minted_active`, `#sweep` (×7); commit `cab07e4` |
| **WM-02** | Never change an existing table or column in `store.py#_SCHEMA` without first building a migration mechanism. Only additive `CREATE … IF NOT EXISTS` objects apply to an existing `/data/events.db`. | No migration path exists: `CREATE IF NOT EXISTS` silently skips an existing table, so a column change ships but never reaches the live volume. Owner decision (2026-11-14): build migrations before the first schema change. | `src/webhooks_mcp/store.py#_SCHEMA`; owner answer 2026-11-14; tracked in findings |

## Rules (`WM-*`) — binding, easy-to-miss conventions

| ID | Rule | Why | Evidence | Example |
|---|---|---|---|---|
| **WM-03** | The 192 MB container budget shapes the code paths. (1) Ingest handlers drop the parsed JSON tree (`del body`) before `await store.insert(...)`. (2) List and wait paths never `SELECT payload` for rows over the inline limit. A new list-like tool or ingest path must do both. | 32 concurrent 256 KiB posts would OOM otherwise. The spec states only the list half. | `src/webhooks_mcp/ingest_app.py#typed` ("del body"), `#minted`, `src/webhooks_mcp/store.py#_INLINE_COLUMNS`; pinned by `tests/test_ingest_app.py#test_requests_waiting_for_the_writer_hold_no_parsed_tree` and `tests/test_mcp_app.py#test_list_events_does_not_load_payloads_it_will_not_inline`; commits `c2cd7df`, `0c5b56d` | `src/webhooks_mcp/ingest_app.py#typed` |
| **WM-04** | Modules read caps as `from webhooks_mcp.constants import X` at **call time**. Never bind a cap into a default argument or a precomputed module value, and never switch to `constants.X` access. | Tests override caps by monkeypatching the name **in the consuming module** (`webhooks_mcp.store.MAX_STORE_BYTES`, …). A precomputed binding makes those tests silently exercise the real 1 GiB / 1000 / 25 s values. `wait_for_event(timeout_s=WAIT_MAX_SECONDS)` is safe only because the clamp re-reads the global. Owner-approved exception (2026-11-15): `DEFAULT_PAGE_SIZE` is bound as the `list_events` default so the MCP input schema advertises it; it is a default, not a cap, and `LIST_MAX_LIMIT` is still clamped at call time. | `tests/test_store.py#test_per_name_cap_holds_on_insert_without_sweep`, `#test_global_byte_budget_evicts_oldest_first`, `tests/test_mcp_app.py#test_wait_timeout_above_max_is_clamped`, `tests/test_main.py#test_sweep_runs_at_start_and_periodically` (10 monkeypatches across 8 tests) | `tests/test_store.py#test_per_name_cap_holds_on_insert_without_sweep` |
| **WM-05** | Two error envelopes, never mixed. HTTP routes return `JSONResponse({"error": <fixed reason>}, status)` and never echo input. MCP tools raise `ToolError("<CODE>: <arg-name-or-id>")` with `INVALID_ARGUMENT` or `NOT_FOUND`. | Payloads are untrusted (CLAUDE.md Rule 4). Echoing the offending value or body re-injects it into an agent's context. | `src/webhooks_mcp/ingest_app.py#_HttpError`, `src/webhooks_mcp/mcp_app.py#_check_name`, `#get_event`, `#wait_for_event`, `#mint_webhook_url`; pinned by `tests/test_ingest_app.py#test_error_does_not_echo_body` | `src/webhooks_mcp/mcp_app.py#_check_name` |
| **WM-06** | The per-name cap has **two** implementations: `_trim_name` (single name, on insert, OFFSET) and the `ROW_NUMBER()` window in `sweep` (bulk). Change the cap semantics in both. A new delete path should resync `_total_bytes` the way `sweep` does (`_sum_bytes`) instead of hand-computing a delta. | Divergent cap semantics make insert and sweep disagree. `sweep` is the self-healing resync for the byte total that `tests/test_store.py#_real_total` checks. | `src/webhooks_mcp/store.py#_trim_name`, `#sweep` ("ROW_NUMBER()"), `#_sum_bytes`; commits `dc7fe7f`, `696d86d` | — |
| **WM-07** | MCP test fixtures are opt-in. `settings`/`auth`/`mcp_http` load only via `pytest_plugins = ["mcp_fixtures"]`, while `conftest.py` holds only `clock`/`store`/`ingest`. A new MCP test file declares the plugin. Never copy the fixtures into conftest. | Without the plugin the test fails with "fixture not found". Copying the fixtures duplicates the test `KEY`. | `tests/mcp_fixtures.py`, `tests/test_mcp_app.py` ("pytest_plugins"), `tests/test_main.py` ("pytest_plugins") | `tests/test_main.py` |
| **WM-08** | The bearer check is an exact byte compare of `b"Bearer " + key`: the scheme is case-sensitive and there is no whitespace trimming. Keep it exact, and keep consumers (homelab-edge Gatus probe, Caddy, MCP clients) sending `Bearer` verbatim. | A proxy or client that normalizes to `bearer` gets 401 (C1). | `src/webhooks_mcp/mcp_app.py#build_mcp_app` ("expected = b\"Bearer \"") | — |
| **WM-09** | `INGEST_BASE_URLS` is a three-way contract. `default` is required, and every label passes verbatim into `mint_webhook_url`'s `urls`. homelab-edge sets the map (`default`, `staging`), and agents and xstockstrat pick by label. Rename a label in all three places or none. | A rename on one side breaks startup (`ConfigError`) or makes agents pick a URL that no longer exists. Confirmed from both sides. | `src/webhooks_mcp/config.py#_parse_bases`, `src/webhooks_mcp/mcp_app.py#mint_webhook_url`; homelab-edge `webhooks_ingest_base_urls` (C4) | — |

## Norms (`WM-*`) — defaults & asymmetry guidance

_None._ The literal-cap outliers were moved into `constants.py` on 2026-11-15, and `CLAUDE.md` Rule 2 now names its
exceptions (listener ports, `_MAX_SEQ`); see `## Resolved` in the findings log.

## Gotchas & scars

- **DNS-rebinding protection is off on purpose.** `TransportSecuritySettings(enable_dns_rebinding_protection=False)`,
  together with `stateless_http=True` and `json_response=True`, is safe because the public Caddy pins the Host and
  the bearer key gates every call (owner answer 2026-11-14). Don't re-enable it without the edge's host list, and
  don't treat this as a template for an unproxied listener. Evidence: `src/webhooks_mcp/mcp_app.py#build_mcp_app`.
- **`X-Forwarded-For` is trusted unconditionally, which is correct only behind the edge.** uvicorn runs with
  `proxy_headers=False` and the app takes the first XFF entry. homelab-edge Caddy configures no `trusted_proxies`,
  so it overwrites any inbound XFF and leaves a single entry (C5, confirmed from both sides). Never configure
  `trusted_proxies` on the ingest site, and never expose 9101 without Caddy: either makes `source_ip` spoofable.
  Evidence: `src/webhooks_mcp/ingest_app.py#_source_ip`, `src/webhooks_mcp/main.py#_server` ("proxy_headers=False");
  homelab-edge `roles/stack/templates/Caddyfile.j2`.
- **Idempotency lasts only as long as the original row.** `_insert_row` dedups by looking up a stored row. Once TTL,
  the per-name cap or the byte budget evicts the original, a late redelivery from xstockstrat's backoff becomes a
  *new* `201` event. Evidence: `src/webhooks_mcp/store.py#_insert_row`.
- **`/hooks/{name}` stores the whole body.** It keeps any `event_name`/`payload` keys and still honours
  `Idempotency-Key`. An agent reading a minted event sent with the typed envelope sees `payload.payload.data`, not
  `payload.data` (spec §8.3, intentional). Evidence: `src/webhooks_mcp/ingest_app.py#minted`.
- **Byte budget charges a per-row overhead.** Before `696d86d`, tiny events with long Idempotency-Keys could fill
  about 1000× the budget on disk (the charge itself: `CLAUDE.md#Invariants an edit can silently break`).
  Evidence: commit `696d86d`, `src/webhooks_mcp/store.py#_evict_oldest`.

## Candidate rules (unverified)

| Candidate | Why suspected | What would confirm it |
|---|---|---|
| Exactly one process per `DATA_DIR` (never scale the service to more than one replica) | The in-process `asyncio.Condition` and `threading.Lock` give no cross-process coordination; spec §6.1 says "single process because wait wakes in-process" | The owner confirms, or homelab-edge compose pins `replicas: 1` explicitly |
| Minted-URL lifetime is intentionally coupled to `EVENT_TTL` | `store.py#mint` derives expiry from the event TTL; spec §6.4 marks it "(call)" | The owner's intent on separating them |
| Long-polling `wait_for_event` calls count against uvicorn `limit_concurrency` (MCP cap 200) | uvicorn counts open connections | uvicorn docs for `limit_concurrency` |
| `tests/test_mcp_app.py` re-declares `NOTICE`/`TOOLS` as a contract pin | Literals duplicated instead of imported | The owner said it was **not** deliberate (2026-11-14); kept here so a future DRY change is a conscious choice |

## Pointers (already documented or CI-enforced — not restated here)

| What | Where |
|---|---|
| Ports, paths, env vars, response codes, tool names, body cap, hostnames, wire format (C1–C9) | `docs/contracts.md` |
| Constants only in `constants.py`; unauthenticated ingest; payload-only plus `NOTICE`; key never logged | `CLAUDE.md#Rules` |
| `AUTOINCREMENT` seq, `_total_bytes` parity, `keep_seq`, injected clock | `CLAUDE.md#Invariants an edit can silently break` |
| Storage, retention, inline limit and memory rationale | `docs/superpowers/specs/2026-11-11-webhooks-mcp-design.md` §6 |
| Release, digest bump, base-image/uv pin parity | `docs/release.md` |
| CI gates: ruff, `pytest -W error`, `uv lock --check`, image smoke (SIGTERM exit 0 under 10 s, uid 10001, restart persistence) | `.github/workflows/ci.yml`, `scripts/smoke.sh` |

---
_Forged by [context-forge](https://github.com/davcs86/agent-plugins). It captures the
non-obvious — nothing here is invented; re-run `/context-constitution` to refresh after the code changes._
